import json
import uuid
from typing import Annotated

from dhi import BaseModel
from turboapi import Depends, Form, Query, TurboAPI


class SearchRequest(BaseModel):
    query: str
    limit: int = 10


class SearchResponse(BaseModel):
    count: int


def get_session():
    return {"session": True}


SessionDep = Annotated[dict, Depends(get_session)]


def test_openapi_is_json_serializable_with_form_defaults():
    app = TurboAPI()

    @app.post("/login")
    def login(username: str = Form(), password: str = Form()):
        return {"username": username}

    schema = app.openapi()

    json.dumps(schema)
    request_body = schema["paths"]["/login"]["post"]["requestBody"]
    assert "application/x-www-form-urlencoded" in request_body["content"]
    properties = request_body["content"]["application/x-www-form-urlencoded"]["schema"][
        "properties"
    ]
    assert set(properties) == {"username", "password"}
    assert not any(
        "turboapi.datastructures.Form object" in str(value)
        for value in properties.values()
    )


def test_openapi_registers_body_and_response_models_in_components():
    app = TurboAPI()

    @app.post("/search", response_model=SearchResponse)
    def search(request: SearchRequest):
        return SearchResponse(count=request.limit)

    schema = app.openapi()
    operation = schema["paths"]["/search"]["post"]

    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SearchRequest"
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SearchResponse"
    }
    assert "SearchRequest" in schema["components"]["schemas"]
    assert "SearchResponse" in schema["components"]["schemas"]


def test_openapi_does_not_emit_dependency_parameters():
    app = TurboAPI()

    @app.post("/search", response_model=SearchResponse)
    def search(session: SessionDep, request: SearchRequest):
        return SearchResponse(count=request.limit)

    operation = app.openapi()["paths"]["/search"]["post"]

    assert "parameters" not in operation
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SearchRequest"
    }


def test_openapi_respects_query_marker_on_post_routes():
    app = TurboAPI()

    @app.post("/search")
    def search(request: SearchRequest, include_archived: bool = Query(default=False)):
        return {"include_archived": include_archived}

    operation = app.openapi()["paths"]["/search"]["post"]

    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SearchRequest"
    }
    assert operation["parameters"] == [
        {
            "name": "include_archived",
            "in": "query",
            "schema": {"type": "boolean", "default": False},
            "required": False,
        }
    ]


def test_openapi_emits_uuid_path_parameter_schema():
    app = TurboAPI()

    @app.delete("/items/{item_id}")
    def delete_item(item_id: uuid.UUID):
        return {"item_id": str(item_id)}

    operation = app.openapi()["paths"]["/items/{item_id}"]["delete"]

    assert operation["parameters"] == [
        {
            "name": "item_id",
            "in": "path",
            "required": True,
            "schema": {"type": "string", "format": "uuid"},
        }
    ]


def test_openapi_deduplicates_operation_ids_only_on_collision():
    app = TurboAPI()

    def first_preflight():
        return {"ok": True}

    def second_preflight():
        return {"ok": True}

    first_preflight.__name__ = "cors_preflight"
    second_preflight.__name__ = "cors_preflight"
    app.options("/first")(first_preflight)
    app.options("/second")(second_preflight)

    schema = app.openapi()

    assert schema["paths"]["/first"]["options"]["operationId"] == "cors_preflight"
    assert (
        schema["paths"]["/second"]["options"]["operationId"]
        == "cors_preflight_options_second"
    )
