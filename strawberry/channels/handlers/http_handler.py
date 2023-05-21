"""GraphQLHTTPHandler

A consumer to provide a graphql endpoint, and optionally graphiql.
"""
from __future__ import annotations

import dataclasses
import json
from io import BytesIO
from typing import TYPE_CHECKING, Any, Mapping, Optional
from urllib.parse import parse_qs

from django.conf import settings
from django.core.files import uploadhandler
from django.http.multipartparser import MultiPartParser

from channels.db import database_sync_to_async
from channels.generic.http import AsyncHttpConsumer
from strawberry.http.async_base_view import AsyncBaseHTTPView, AsyncHTTPRequestAdapter
from strawberry.http.exceptions import HTTPException
from strawberry.http.temporal_response import TemporalResponse
from strawberry.http.types import FormData
from strawberry.http.typevars import Context, RootValue
from strawberry.types.graphql import OperationType
from strawberry.utils.graphiql import get_graphiql_html

from .base import ChannelsConsumer

if TYPE_CHECKING:
    from strawberry.http import GraphQLHTTPResponse, GraphQLRequestData
    from strawberry.http.types import HTTPMethod, QueryParams
    from strawberry.schema import BaseSchema


class MethodNotAllowed(Exception):
    ...


class ExecutionError(Exception):
    ...


@dataclasses.dataclass
class Result:
    response: bytes
    status: int = 200
    content_type: str = "application/json"
    headers: Mapping[bytes, bytes] | None = None


@dataclasses.dataclass
class Request:
    consumer: ChannelsConsumer
    body: bytes


class ChannelsRequestAdapter(AsyncHTTPRequestAdapter):
    def __init__(self, request: Request):
        self.request = request

    @property
    def query_params(self) -> QueryParams:
        query_params_str = self.request.consumer["query_string"].decode()

        query_params = {}
        for key, value in parse_qs(query_params_str, keep_blank_values=True).items():
            # Only one argument per key is expected here
            query_params[key] = value[0]

        return query_params

    @property
    def method(self) -> HTTPMethod:
        return self.request.consumer["method"].upper()

    @property
    def headers(self) -> Mapping[str, str]:
        return {
            header_name.decode().lower(): header_value.decode()
            for header_name, header_value in self.request.consumer["headers"]
        }

    @property
    def content_type(self) -> Optional[str]:
        return self.headers.get("content-type", None)

    async def get_body(self) -> bytes:
        return self.request.body

    async def get_form_data(self) -> FormData:
        upload_handlers = [
            uploadhandler.load_handler(handler)
            for handler in settings.FILE_UPLOAD_HANDLERS
        ]

        parser = MultiPartParser(
            {
                "CONTENT_TYPE": self.headers.get("content-type"),
                "CONTENT_LENGTH": self.headers.get("content-length", "0"),
            },
            BytesIO(self.request.body),
            upload_handlers,
        )

        querydict, files = parser.parse()

        form = {
            "operations": json.loads(querydict.get("operations", "")),
            "map": json.loads(querydict.get("map", "")),
        }

        return FormData(files=files, form=form)


class GraphQLHTTPConsumer(
    AsyncBaseHTTPView[
        Request,
        Result,
        TemporalResponse,
        Context,
        RootValue,
    ],
    ChannelsConsumer,
    AsyncHttpConsumer,
):
    """A consumer to provide a view for GraphQL over HTTP.

    To use this, place it in your ProtocolTypeRouter for your channels project:

    ```
    from strawberry.channels import GraphQLHttpRouter
    from channels.routing import ProtocolTypeRouter
    from django.core.asgi import get_asgi_application

    application = ProtocolTypeRouter({
        "http": URLRouter([
            re_path("^graphql", GraphQLHTTPRouter(schema=schema)),
            re_path("^", get_asgi_application()),
        ]),
        "websocket": URLRouter([
            re_path("^ws/graphql", GraphQLWebSocketRouter(schema=schema)),
        ]),
    })
    ```
    """

    allow_queries_via_get: bool = True
    request_adapter_class = ChannelsRequestAdapter

    def __init__(
        self,
        schema: BaseSchema,
        graphiql: bool = True,
        allow_queries_via_get: bool = True,
        subscriptions_enabled: bool = True,
        **kwargs: Any,
    ):
        self.schema = schema
        self.graphiql = graphiql
        self.allow_queries_via_get = allow_queries_via_get
        self.subscriptions_enabled = subscriptions_enabled
        super().__init__(**kwargs)

    async def handle(self, body: bytes) -> None:
        request = Request(consumer=self.scope, body=body)
        try:
            response = await self.run(request)

            if response.headers is None:
                response.headers = {}

            if b"Content-Type" not in response.headers:
                response.headers[b"Content-Type"] = response.content_type.encode()

            await self.send_response(
                response.status,
                response.response,
                headers=response.headers,
            )
        except HTTPException as e:
            await self.send_response(e.status_code, e.reason.encode())

    def create_response(
        self, response_data: GraphQLHTTPResponse, sub_response: TemporalResponse
    ) -> Result:
        result = Result(response=json.dumps(response_data).encode())

        if sub_response.status_code:
            result.status = sub_response.status_code

        if sub_response.headers:
            result.headers = {
                k.encode(): v.encode() for k, v in sub_response.headers.items()
            }

        return result

    async def get_root_value(self, request: Request) -> Optional[RootValue]:
        return None

    async def get_context(
        self, request: Request, response: TemporalResponse
    ) -> Context:
        return {
            "request": request,
            "response": response,
        }  # type: ignore

    async def get_sub_response(self, request: Request) -> TemporalResponse:
        return TemporalResponse()

    def render_graphiql(self, request: Request) -> Result:
        html = get_graphiql_html(self.subscriptions_enabled)
        return Result(response=html.encode(), content_type="text/html")


class SyncGraphQLHTTPConsumer(GraphQLHTTPConsumer):
    """Synchronous version of the HTTPConsumer.

    This is the same as `GraphQLHTTPConsumer`, but it can be used with
    synchronous schemas (i.e. the schema's resolvers are expected to be
    synchronous and not asynchronous).
    """

    # Sync channels is actually async, but it uses database_sync_to_async to call
    # handlers in a threadpool. Check SyncConsumer's documentation for more info:
    # https://github.com/django/channels/blob/main/channels/consumer.py#L104
    @database_sync_to_async
    def execute(self, request_data: GraphQLRequestData) -> GraphQLHTTPResponse:
        context = self.get_context(self)
        root_value = self.get_root_value(self)

        method = self.scope["method"]
        allowed_operation_types = OperationType.from_http(method)
        if not self.allow_queries_via_get and method == "GET":
            allowed_operation_types = allowed_operation_types - {OperationType.QUERY}

        result = self.schema.execute_sync(
            query=request_data.query,
            root_value=root_value,
            variable_values=request_data.variables,
            context_value=context,
            operation_name=request_data.operation_name,
            allowed_operation_types=allowed_operation_types,
        )
        return self.process_result(result)
