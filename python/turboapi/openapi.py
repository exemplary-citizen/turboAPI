"""OpenAPI schema generation and Swagger/ReDoc UI for TurboAPI.

Generates OpenAPI 3.1.0 compatible schemas from route definitions and serves
interactive API documentation at /docs (Swagger UI) and /redoc (ReDoc).
"""

import inspect
import json
import re
import uuid
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from .datastructures import Body, Cookie, File, Form, Header, Query, UploadFile
from .datastructures import Path as PathParam
from .security import SecurityBase, get_depends

PARAM_MARKERS = (Body, Cookie, File, Form, Header, PathParam, Query)
BODY_MARKERS = (Body, File, Form)


def generate_openapi_schema(app) -> dict:
    """Generate OpenAPI 3.1.0 schema from app routes.

    Args:
        app: TurboAPI application instance.

    Returns:
        OpenAPI schema dict.
    """
    components = {
        "HTTPValidationError": {
            "title": "HTTPValidationError",
            "type": "object",
            "properties": {
                "detail": {
                    "title": "Detail",
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/ValidationError"},
                }
            },
        },
        "ValidationError": {
            "title": "ValidationError",
            "type": "object",
            "properties": {
                "loc": {
                    "title": "Location",
                    "type": "array",
                    "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                },
                "msg": {"title": "Message", "type": "string"},
                "type": {"title": "Error Type", "type": "string"},
            },
            "required": ["loc", "msg", "type"],
        },
    }

    schema = {
        "openapi": "3.1.0",
        "info": {
            "title": getattr(app, "title", "TurboAPI"),
            "version": getattr(app, "version", "0.1.0"),
            "description": getattr(app, "description", ""),
        },
        "paths": {},
        "components": {"schemas": components},
    }

    routes = app.registry.get_routes()
    operation_ids: set[str] = set()
    for route in routes:
        path = route.path
        method = route.method.value.lower()
        handler = route.handler

        # Generate operation
        operation = _generate_operation(handler, route, components)
        operation["operationId"] = _unique_operation_id(
            operation["operationId"],
            method,
            path,
            operation_ids,
        )

        # Add to paths
        openapi_path = _convert_path(path)
        if openapi_path not in schema["paths"]:
            schema["paths"][openapi_path] = {}
        schema["paths"][openapi_path][method] = operation

    return schema


def _convert_path(path: str) -> str:
    """Convert route path to OpenAPI format (already uses {param} syntax)."""
    return path


def _generate_operation(handler, route, components: dict[str, Any]) -> dict:
    """Generate OpenAPI operation object from handler."""
    operation: dict[str, Any] = {
        "summary": _get_summary(handler),
        "operationId": handler.__name__,
        "responses": {
            "200": {
                "description": "Successful Response",
                "content": {
                    "application/json": {
                        "schema": _type_to_schema(route.response_model, components)
                        if getattr(route, "response_model", None)
                        else {}
                    }
                },
            },
            "422": {
                "description": "Validation Error",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
                    }
                },
            },
        },
    }

    # Extract parameters from signature
    sig = inspect.signature(handler)
    parameters = []
    body_params = []

    path_params = set(re.findall(r"\{([^}]+)\}", route.path))

    for param_name, param in sig.parameters.items():
        if get_depends(param) is not None or isinstance(param.default, SecurityBase):
            continue

        annotation = _unwrap_annotated(param.annotation)
        marker = param.default if isinstance(param.default, PARAM_MARKERS) else None
        param_schema = _type_to_schema(annotation, components)
        default = _param_default(param, marker)
        required = default is inspect.Parameter.empty or default is ...
        openapi_name = getattr(marker, "alias", None) or param_name
        if marker is not None:
            _apply_marker_metadata(param_schema, marker)

        if param_name in path_params:
            parameters.append(
                {
                    "name": openapi_name,
                    "in": "path",
                    "required": True,
                    "schema": param_schema,
                }
            )
        elif _parameter_location(marker, route) == "body":
            body_params.append(
                {
                    "name": openapi_name,
                    "schema": param_schema,
                    "required": required,
                    "marker": marker,
                    "annotation": annotation,
                }
            )
        else:
            query_param = {
                "name": openapi_name,
                "in": _parameter_location(marker, route),
                "schema": param_schema,
                "required": required,
            }
            if default is not inspect.Parameter.empty and default is not ... and default is not None:
                _set_json_default(query_param["schema"], default)
            parameters.append(query_param)

    if parameters:
        operation["parameters"] = parameters

    if body_params:
        media_type = _request_body_media_type(body_params)
        body_schema: dict[str, Any]
        if _should_use_direct_body_schema(body_params, media_type):
            body_schema = body_params[0]["schema"]
        else:
            body_schema = {
                "type": "object",
                "properties": {item["name"]: item["schema"] for item in body_params},
            }
            required = [item["name"] for item in body_params if item["required"]]
            if required:
                body_schema["required"] = required

        operation["requestBody"] = {
            "required": any(item["required"] for item in body_params),
            "content": {
                media_type: {"schema": body_schema}
            },
        }

    # Add tags
    if hasattr(route, "tags") and route.tags:
        operation["tags"] = route.tags

    # Add docstring as description
    if handler.__doc__:
        operation["description"] = handler.__doc__.strip()

    return operation


def _get_summary(handler) -> str:
    """Generate summary from handler name."""
    name = handler.__name__
    return name.replace("_", " ").title()


def _unique_operation_id(
    operation_id: str, method: str, path: str, used_ids: set[str]
) -> str:
    if operation_id not in used_ids:
        used_ids.add(operation_id)
        return operation_id

    path_suffix = re.sub(r"[^0-9a-zA-Z]+", "_", path).strip("_").lower()
    candidate = f"{operation_id}_{method}_{path_suffix}"
    counter = 2
    while candidate in used_ids:
        candidate = f"{operation_id}_{method}_{path_suffix}_{counter}"
        counter += 1
    used_ids.add(candidate)
    return candidate


def _unwrap_annotated(annotation):
    if get_origin(annotation) is Annotated:
        return get_args(annotation)[0]
    return annotation


def _parameter_location(marker, route) -> str:
    if isinstance(marker, BODY_MARKERS):
        return "body"
    if isinstance(marker, Query):
        return "query"
    if isinstance(marker, Header):
        return "header"
    if isinstance(marker, Cookie):
        return "cookie"
    if route.method.value.upper() in ("POST", "PUT", "PATCH"):
        return "body"
    return "query"


def _request_body_media_type(body_params: list[dict[str, Any]]) -> str:
    if any(isinstance(item["marker"], File) for item in body_params):
        return "multipart/form-data"
    if any(isinstance(item["marker"], Form) for item in body_params):
        return "application/x-www-form-urlencoded"
    return "application/json"


def _should_use_direct_body_schema(body_params: list[dict[str, Any]], media_type: str) -> bool:
    if media_type != "application/json" or len(body_params) != 1:
        return False
    marker = body_params[0]["marker"]
    if isinstance(marker, Body) and marker.embed:
        return False
    schema = body_params[0]["schema"]
    return "$ref" in schema


def _param_default(param: inspect.Parameter, marker) -> Any:
    if marker is not None:
        return marker.default
    return param.default


def _set_json_default(schema: dict[str, Any], value: Any) -> None:
    try:
        json.dumps(value)
    except TypeError:
        return
    schema["default"] = value


def _apply_marker_metadata(schema: dict[str, Any], marker) -> None:
    for attr, key in (("title", "title"), ("description", "description")):
        value = getattr(marker, attr, None)
        if value is not None:
            schema[key] = value
    for attr, key in (
        ("min_length", "minLength"),
        ("max_length", "maxLength"),
        ("regex", "pattern"),
        ("gt", "exclusiveMinimum"),
        ("ge", "minimum"),
        ("lt", "exclusiveMaximum"),
        ("le", "maximum"),
    ):
        value = getattr(marker, attr, None)
        if value is not None:
            schema[key] = value


def _type_to_schema(annotation, components: dict[str, Any]) -> dict:
    """Convert Python type annotation to OpenAPI schema."""
    annotation = _unwrap_annotated(annotation)
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is list:
        return {"type": "array", "items": {}}
    if annotation is dict:
        return {"type": "object"}
    if annotation is bytes:
        return {"type": "string", "format": "binary"}
    if annotation is uuid.UUID:
        return {"type": "string", "format": "uuid"}
    if annotation is UploadFile:
        return {"type": "string", "format": "binary"}

    # Handle typing generics
    origin = get_origin(annotation)
    if origin is list:
        args = get_args(annotation)
        items_schema = _type_to_schema(args[0], components) if args else {}
        return {"type": "array", "items": items_schema}
    if origin is dict:
        return {"type": "object"}

    # Handle Optional[X] / Union[X, None] — get_origin returns Union, not type(None)
    if origin in (Union, UnionType):
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            inner = _type_to_schema(non_none[0], components)
            inner["nullable"] = True
            return inner
        return {"nullable": True}
    # Handle bare NoneType annotation
    if annotation is type(None):
        return {"nullable": True}

    # Try to get schema from Satya/Pydantic models
    try:
        if (
            hasattr(annotation, "__fields__")
            or hasattr(annotation, "model_fields")
            or hasattr(annotation, "model_json_schema")
        ):
            return _model_ref(annotation, components)
    except (TypeError, AttributeError):
        pass

    return {}


def _model_ref(model, components: dict[str, Any]) -> dict:
    name = model.__name__
    if name not in components:
        components[name] = {}
        if hasattr(model, "model_json_schema"):
            try:
                schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
            except TypeError:
                schema = model.model_json_schema()
        else:
            try:
                schema = model.schema(ref_template="#/components/schemas/{model}")
            except TypeError:
                schema = model.schema()
        defs = schema.pop("$defs", None) or schema.pop("definitions", None) or {}
        for def_name, def_schema in defs.items():
            components.setdefault(def_name, def_schema)
        components[name] = schema
    return {"$ref": f"#/components/schemas/{name}"}


# HTML templates for Swagger UI and ReDoc
SWAGGER_UI_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>{title} - Swagger UI</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet" type="text/css" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
<div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>
SwaggerUIBundle({{
    url: "{openapi_url}",
    dom_id: '#swagger-ui',
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
    layout: "BaseLayout"
}})
</script>
</body>
</html>"""

REDOC_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>{title} - ReDoc</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700" rel="stylesheet">
    <style>body {{ margin: 0; padding: 0; }}</style>
</head>
<body>
<redoc spec-url='{openapi_url}'></redoc>
<script src="https://unpkg.com/redoc@latest/bundles/redoc.standalone.js"></script>
</body>
</html>"""


def get_swagger_ui_html(title: str, openapi_url: str = "/openapi.json") -> str:
    """Generate Swagger UI HTML page."""
    return SWAGGER_UI_HTML.format(title=title, openapi_url=openapi_url)


def get_redoc_html(title: str, openapi_url: str = "/openapi.json") -> str:
    """Generate ReDoc HTML page."""
    return REDOC_HTML.format(title=title, openapi_url=openapi_url)
