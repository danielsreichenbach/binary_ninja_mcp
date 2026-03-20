import sys as _sys
import traceback as _tb


# Install a very-early excepthook so any ImportError at module import time is captured.
def _bridge_excepthook(exc_type, exc, tb):
    # Print to stderr for interactive runs
    _tb.print_exception(exc_type, exc, tb, file=_sys.stderr)


_sys.excepthook = _bridge_excepthook

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP

binja_server_url = "http://localhost:9009"
mcp = FastMCP("binaryninja")

# Thread pool for HTTP requests to Binary Ninja.
# Binary Ninja's plugin HTTP server is single-threaded, so we use a
# serializing semaphore to avoid overwhelming it while still allowing
# multiple MCP tool calls to be queued concurrently. This prevents one
# slow decompilation from blocking the entire MCP server.
_http_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="binja-http")
_binja_semaphore = threading.Semaphore(1)


def _do_get(url: str, timeout: float | None) -> requests.Response:
    """Execute a GET request under the Binary Ninja semaphore."""
    with _binja_semaphore:
        if timeout is None:
            return requests.get(url)
        return requests.get(url, timeout=timeout)


def _do_post(url: str, timeout: float | None, **kwargs) -> requests.Response:
    """Execute a POST request under the Binary Ninja semaphore."""
    with _binja_semaphore:
        if timeout is None:
            return requests.post(url, **kwargs)
        return requests.post(url, timeout=timeout, **kwargs)


async def _async_get(url: str, timeout: float | None) -> requests.Response:
    """Run a GET request in the thread pool without blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_http_pool, _do_get, url, timeout)


async def _async_post(url: str, timeout: float | None, **kwargs) -> requests.Response:
    """Run a POST request in the thread pool without blocking the event loop."""
    loop = asyncio.get_running_loop()
    import functools
    fn = functools.partial(_do_post, url, timeout, **kwargs)
    return await loop.run_in_executor(_http_pool, fn)


def _active_filename() -> str:
    """Return the currently active filename as known by the server."""
    try:
        st = get_json("status")
        if isinstance(st, dict) and st.get("filename"):
            return str(st.get("filename"))
    except Exception:
        pass
    return "(none)"


def _build_url(endpoint: str, params: dict | None = None) -> str:
    """Build a full URL with query string from endpoint and params."""
    if params is None:
        params = {}
    qs = [f"{k}={v}" for k, v in params.items()]
    query_string = "&".join(qs)
    url = f"{binja_server_url}/{endpoint}"
    if query_string:
        url += "?" + query_string
    return url


def safe_get(endpoint: str, params: dict | None = None, timeout: float | None = 30) -> list:
    """
    Perform a GET request. If 'params' is given, we convert it to a query string.
    Runs in a thread pool so it does not block the async event loop.
    """
    url = _build_url(endpoint, params)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    try:
        if loop and loop.is_running():
            # Called from async context -- cannot use .result() directly,
            # but FastMCP tool functions are sync, called from a thread
            # via run_in_executor already. Use direct blocking call with
            # the semaphore to serialize access to BinaryNinja.
            response = _do_get(url, timeout)
        else:
            response = _do_get(url, timeout)
        response.encoding = "utf-8"
        if response.ok:
            return response.text.splitlines()
        else:
            return [f"Error {response.status_code}: {response.text.strip()}"]
    except Exception as e:
        return [f"Request failed: {e!s}"]


def get_json(endpoint: str, params: dict | None = None, timeout: float | None = 30) -> Any:
    """
    Perform a GET and return parsed JSON.
    - On 2xx: returns parsed JSON.
    - On 4xx/5xx: attempts to parse JSON body and return it; if not JSON, returns {'error': 'Error <code>: <text>'}.
    Returns None only on transport errors.
    Serialized through the BinaryNinja semaphore.
    """
    url = _build_url(endpoint, params)
    try:
        response = _do_get(url, timeout)
        response.encoding = "utf-8"
        # Try to parse JSON regardless of status
        try:
            data = response.json()
        except Exception:
            data = None
        if response.ok:
            return data
        # Non-OK: return parsed error object if available; otherwise synthesize one
        if isinstance(data, dict):
            # Ensure at least an error field for LLMs
            if "error" not in data:
                data = {"error": str(data)}
            data.setdefault("status", str(response.status_code))
            return data
        text = (response.text or "").strip()
        return {"error": f"Error {response.status_code}: {text}"}
    except Exception as e:
        return {"error": f"Request failed: {e!s}"}


def get_text(endpoint: str, params: dict | None = None, timeout: float | None = 30) -> str:
    """Perform a GET and return raw text (or an error string).
    Serialized through the BinaryNinja semaphore."""
    url = _build_url(endpoint, params)
    try:
        response = _do_get(url, timeout)
        response.encoding = "utf-8"
        if response.ok:
            return response.text
        else:
            return f"Error {response.status_code}: {response.text.strip()}"
    except Exception as e:
        return f"Request failed: {e!s}"


def safe_post(endpoint: str, data: dict | str) -> str:
    """Perform a POST request. Serialized through the BinaryNinja semaphore."""
    try:
        if isinstance(data, dict):
            response = _do_post(f"{binja_server_url}/{endpoint}", 30, data=data)
        else:
            response = _do_post(
                f"{binja_server_url}/{endpoint}", 30, data=data.encode("utf-8")
            )
        response.encoding = "utf-8"
        if response.ok:
            return response.text.strip()
        else:
            return f"Error {response.status_code}: {response.text.strip()}"
    except Exception as e:
        return f"Request failed: {e!s}"


@mcp.tool()
def list_methods(offset: int = 0, limit: int = 100) -> list:
    """
    List all function names in the program with pagination.
    """
    header = f"File: {_active_filename()}"
    body = safe_get("methods", {"offset": offset, "limit": limit})
    return [header] + (body or [])


@mcp.tool()
def get_entry_points() -> list:
    """
    List entry point(s) of the loaded binary.
    """
    data = get_json("entryPoints")
    if not data or "entry_points" not in data:
        return ["Error: no response"]
    out: list[str] = []
    for ep in data.get("entry_points", []) or []:
        addr = ep.get("address")
        name = ep.get("name") or "(unknown)"
        out.append(f"{addr}\t{name}")
    return out


@mcp.tool()
def retype_variable(function_name: str, variable_name: str, type_str: str) -> str:
    """
    Retype a variable in a function.
    """
    data = get_json(
        "retypeVariable",
        {
            "functionName": function_name,
            "variableName": variable_name,
            "type": type_str,
        },
    )
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and "status" in data:
        return data["status"]
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    return str(data)


@mcp.tool()
def rename_single_variable(function_name: str, variable_name: str, new_name: str) -> str:
    """
    Rename a variable in a function.
    """
    data = get_json(
        "renameVariable",
        {
            "functionName": function_name,
            "variableName": variable_name,
            "newName": new_name,
        },
    )
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and "status" in data:
        return data["status"]
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    return str(data)


@mcp.tool()
def rename_multi_variables(
    function_identifier: str,
    mapping_json: str = "",
    pairs: str = "",
    renames_json: str = "",
) -> str:
    """
    Rename multiple local variables in one call.
    - function_identifier: function name or address (hex)
    - Provide either mapping_json (JSON object old->new), renames_json (JSON array of {old,new}), or pairs ("old1:new1,old2:new2").
    Returns per-item results and totals.
    """
    params: dict[str, object] = {}
    ident = (function_identifier or "").strip()
    if ident.lower().startswith("0x") or ident.isdigit():
        params["address"] = ident
    else:
        params["functionName"] = ident

    payload = None
    import json as _json

    if renames_json:
        try:
            payload = _json.loads(renames_json)
        except Exception:
            return "Error: renames_json is not valid JSON"
        params["renames"] = payload
    elif mapping_json:
        try:
            payload = _json.loads(mapping_json)
        except Exception:
            return "Error: mapping_json is not valid JSON"
        params["mapping"] = payload
    elif pairs:
        params["pairs"] = pairs
    else:
        return "Error: provide mapping_json, renames_json, or pairs"

    data = get_json("renameVariables", params)
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    try:
        total = data.get("total")
        renamed = data.get("renamed")
        return f"Batch rename: {renamed}/{total} applied"
    except Exception:
        return str(data)


@mcp.tool()
def define_types(c_code: str) -> str:
    """
    Define types from a C code string.
    """
    data = get_json("defineTypes", {"cCode": c_code})
    if not data:
        return "Error: no response"
    # Expect a list of defined type names or a dict; normalize to string
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    if isinstance(data, (list, tuple)):
        return "Defined types: " + ", ".join(map(str, data))
    return str(data)


@mcp.tool()
def list_classes(offset: int = 0, limit: int = 100) -> list:
    """
    List all namespace/class names in the program with pagination.
    """
    return safe_get("classes", {"offset": offset, "limit": limit})


@mcp.tool()
def hexdump_address(address: str, length: int = -1) -> str:
    """
    Hexdump data starting at an address. When length < 0, reads the exact defined size if available.
    """
    params: dict[str, str] = {"address": address}
    if length is not None:
        params["length"] = str(length)
    return get_text("hexdump", params, timeout=None)


@mcp.tool()
def hexdump_data(name_or_address: str, length: int = -1) -> str:
    """
    Hexdump a data symbol by name or address. When length < 0, reads the exact defined size if available.
    """
    ident = (name_or_address or "").strip()
    if ident.startswith("0x"):
        return hexdump_address(ident, length)
    return get_text("hexdumpByName", {"name": ident, "length": str(length)}, timeout=None)


@mcp.tool()
def get_data_decl(name_or_address: str, length: int = -1) -> str:
    """
    Return a declaration-like string and a hexdump for a data symbol by name or address.
    LLM-friendly: includes both a C-like declaration (when possible) and text hexdump.
    """
    ident = (name_or_address or "").strip()
    params: dict[str, str] = {"name": ident} if not ident.startswith("0x") else {"address": ident}
    if length is not None:
        params["length"] = str(length)
    data = get_json("getDataDecl", params, timeout=None)
    if not data:
        return "Error: no response"
    if "error" in data:
        return f"Error: {data.get('error')}"
    decl = data.get("decl") or "(no declaration)"
    hexdump = data.get("hexdump") or ""
    addr = data.get("address", "")
    name = data.get("name", ident)
    return f"Declaration ({addr} {name}):\n{decl}\n\nHexdump:\n{hexdump}"


@mcp.tool()
def decompile_function(name: str) -> str:
    """
    Decompile a specific function by name and return the decompiled C code.
    """
    file_line = f"File: {_active_filename()}\n\n"
    data = get_json("decompile", {"name": name}, timeout=None)
    if not data:
        return file_line + "Error: no response"
    if "decompiled" in data:
        return file_line + data["decompiled"]
    if "error" in data:
        return file_line + f"Error: {data.get('error')}"
    return file_line + str(data)


@mcp.tool()
def get_il(name_or_address: str, view: str = "hlil", ssa: bool = False) -> str:
    """
    Get IL for a function in the selected view.
    - view: one of hlil, mlil, llil
    - ssa: set True to request SSA form (MLIL/LLIL only)
    """
    file_line = f"File: {_active_filename()}\n\n"
    ident = (name_or_address or "").strip()
    params = {"view": view, "ssa": int(bool(ssa))}
    if ident.lower().startswith("0x") or ident.isdigit():
        params["address"] = ident
    else:
        params["name"] = ident
    data = get_json("il", params, timeout=None)
    if not data:
        return file_line + "Error: no response"
    if "il" in data:
        return file_line + data["il"]
    if "error" in data:
        import json as _json

        return file_line + _json.dumps(data, indent=2, ensure_ascii=False)
    return file_line + str(data)


@mcp.tool()
def fetch_disassembly(name: str) -> str:
    """
    Retrive the disassembled code of a function with a given name as assemby mnemonic instructions.
    """
    file_line = f"File: {_active_filename()}\n\n"
    data = get_json("assembly", {"name": name}, timeout=None)
    if not data:
        return file_line + "Error: no response"
    if "assembly" in data:
        return file_line + data["assembly"]
    if "error" in data:
        return file_line + f"Error: {data.get('error')}"
    return file_line + str(data)


@mcp.tool()
def rename_function(old_name: str, new_name: str) -> str:
    """
    Rename a function by its current name to a new user-defined name.
    The configured prefix (default "mcp_") will be automatically prepended if not present.
    """
    return safe_post("renameFunction", {"oldName": old_name, "newName": new_name})


@mcp.tool()
def rename_data(address: str, new_name: str) -> str:
    """
    Rename a data label at the specified address.
    """
    return safe_post("renameData", {"address": address, "newName": new_name})


@mcp.tool()
def set_comment(address: str, comment: str) -> str:
    """
    Set a comment at a specific address.
    """
    return safe_post("comment", {"address": address, "comment": comment})


@mcp.tool()
def set_function_comment(function_name: str, comment: str) -> str:
    """
    Set a comment for a function.
    """
    return safe_post("comment/function", {"name": function_name, "comment": comment})


@mcp.tool()
def get_comment(address: str) -> str:
    """
    Get the comment at a specific address.
    """
    return safe_get("comment", {"address": address})[0]


@mcp.tool()
def get_function_comment(function_name: str) -> str:
    """
    Get the comment for a function.
    """
    return safe_get("comment/function", {"name": function_name})[0]


@mcp.tool()
def list_segments(offset: int = 0, limit: int = 100) -> list:
    """
    List all memory segments in the program with pagination.
    """
    return safe_get("segments", {"offset": offset, "limit": limit})


@mcp.tool()
def list_sections(offset: int = 0, limit: int = 100) -> list:
    """
    List sections in the program with pagination.

    Returns one line per section with: start-end, size, name, and any semantics/type if available.
    """
    data = get_json("sections", {"offset": offset, "limit": limit})
    if not data or not isinstance(data, dict):
        return ["Error: no response"]
    if data.get("error"):
        return [f"Error: {data.get('error')}"]
    sections = data.get("sections", []) or []
    out: list[str] = [f"File: {_active_filename()}"]
    for s in sections:
        try:
            start = s.get("start") or ""
            end = s.get("end") or ""
            size = s.get("size")
            name = s.get("name") or "(unnamed)"
            sem = s.get("semantics") or s.get("type") or ""
            tail = f"\t{sem}" if sem else ""
            out.append(f"{start}-{end}\t{size}\t{name}{tail}")
        except Exception:
            continue
    return out


@mcp.tool()
def list_imports(offset: int = 0, limit: int = 100) -> list:
    """
    List imported symbols in the program with pagination.
    """
    return safe_get("imports", {"offset": offset, "limit": limit})


@mcp.tool()
def list_strings(offset: int = 0, count: int = 100) -> list:
    """
    List all strings in the database (paginated).
    """
    return safe_get("strings", {"offset": offset, "limit": count}, timeout=None)


@mcp.tool()
def list_strings_filter(offset: int = 0, count: int = 100, filter: str = "") -> list:
    """
    List matching strings in the database (paginated, filtered).
    """
    return safe_get(
        "strings/filter",
        {"offset": offset, "limit": count, "filter": filter},
        timeout=None,
    )


@mcp.tool()
def list_local_types(offset: int = 0, count: int = 200, include_libraries: bool = False) -> list:
    """
    List all local types in the database (paginated).
    """
    return safe_get(
        "localTypes",
        {
            "offset": offset,
            "limit": count,
            "includeLibraries": int(bool(include_libraries)),
        },
        timeout=None,
    )


@mcp.tool()
def search_types(
    query: str, offset: int = 0, count: int = 200, include_libraries: bool = False
) -> list:
    """
    Search local types whose name or declaration contains the substring.
    """
    return safe_get(
        "searchTypes",
        {
            "query": query,
            "offset": offset,
            "limit": count,
            "includeLibraries": int(bool(include_libraries)),
        },
        timeout=None,
    )


@mcp.tool()
def list_all_strings(batch_size: int = 500) -> list:
    """
    List all strings in the database (aggregated across pages).
    """
    results: list[str] = []
    offset = 0
    while True:
        data = get_json("strings", {"offset": offset, "limit": batch_size}, timeout=None)
        if not data or "strings" not in data:
            break
        items = data.get("strings", [])
        if not items:
            break
        for s in items:
            addr = s.get("address")
            length = s.get("length")
            stype = s.get("type")
            value = s.get("value")
            results.append(f"{addr}\t{length}\t{stype}\t{value}")
        if len(items) < batch_size:
            break
        offset += batch_size
    return results


@mcp.tool()
def list_exports(offset: int = 0, limit: int = 100) -> list:
    """
    List exported functions/symbols with pagination.
    """
    return safe_get("exports", {"offset": offset, "limit": limit})


@mcp.tool()
def list_namespaces(offset: int = 0, limit: int = 100) -> list:
    """
    List all non-global namespaces in the program with pagination.
    """
    return safe_get("namespaces", {"offset": offset, "limit": limit})


@mcp.tool()
def list_data_items(offset: int = 0, limit: int = 100, filter: str = "") -> list:
    """
    List defined data labels and their values with pagination.
    When filter is non-empty, only items whose name contains the substring
    (case-insensitive) are returned.
    """
    return safe_get("data", {"offset": offset, "limit": limit, "filter": filter})


@mcp.tool()
def search_data_items(query: str, offset: int = 0, limit: int = 100) -> list:
    """
    Search for data labels whose name contains the given substring.
    """
    if not query:
        return ["Error: query string is required"]
    return safe_get("data", {"offset": offset, "limit": limit, "filter": query})


@mcp.tool()
def search_functions_by_name(query: str, offset: int = 0, limit: int = 100) -> list:
    """
    Search for functions whose name contains the given substring.
    """
    if not query:
        return ["Error: query string is required"]
    return safe_get("searchFunctions", {"query": query, "offset": offset, "limit": limit})


@mcp.tool()
def get_binary_status() -> str:
    """
    Get the current status of the loaded binary.
    """
    return safe_get("status")[0]


@mcp.tool()
def list_binaries() -> list:
    """
    List managed/open binaries known to the server with ids and active flag.
    """
    data = get_json("binaries")
    if not data:
        return ["Error: no response"]
    if isinstance(data, dict) and data.get("error"):
        return [data.get("error")]
    items = data.get("binaries", [])
    out = []
    for it in items:
        vid = it.get("id")
        view_id = it.get("view_id")
        fn = it.get("filename")
        basename = it.get("basename") or ""
        selectors = it.get("selectors") or []
        active = it.get("active")
        label = basename or fn or "(unknown)"
        full = fn or "(no filename)"
        selector_text = ", ".join(str(s) for s in selectors if s)
        mark = " *active*" if active else ""
        view_part = f" view={view_id}" if view_id else ""
        out.append(
            f"{vid}. {label}{view_part}{mark}\n    path: {full}\n    selectors: {selector_text}"
        )
    return out


@mcp.tool()
def select_binary(view: str) -> str:
    """
    Select which binary to analyze by ordinal, internal view id, full path, or basename.
    Call this after listing binaries whenever you need to switch analysis targets.
    """
    data = get_json("selectBinary", {"view": view})
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        import json as _json

        return _json.dumps(data, indent=2, ensure_ascii=False)
    sel = data.get("selected") if isinstance(data, dict) else None
    if sel:
        ordinal = sel.get("id") or "?"
        view_id = sel.get("view_id") or ""
        fn = sel.get("filename") or ""
        basename = sel.get("basename") or ""
        selectors = sel.get("selectors") or []
        selector_text = ", ".join(str(s) for s in selectors if s)
        display_name = basename or fn or "(unknown)"
        view_part = f" (view {view_id})" if view_id else ""
        path_part = f"\nFull path: {fn}" if fn else ""
        return (
            f"Selected {ordinal}: {display_name}{view_part}{path_part}\nSelectors: {selector_text}"
        )
    return str(data)


@mcp.tool()
def delete_comment(address: str) -> str:
    """
    Delete the comment at a specific address.
    """
    return safe_post("comment", {"address": address, "_method": "DELETE"})


@mcp.tool()
def delete_function_comment(function_name: str) -> str:
    """
    Delete the comment for a function.
    """
    return safe_post("comment/function", {"name": function_name, "_method": "DELETE"})


@mcp.tool()
def function_at(address: str) -> list:
    """
    Retrive the name of the function the address belongs to. Address must be in hexadecimal format 0x00001
    """
    return safe_get("functionAt", {"address": address})


@mcp.tool()
def get_user_defined_type(type_name: str) -> list:
    """
    Retrive definition of a user defined type (struct, enumeration, typedef, union)
    """
    return safe_get("getUserDefinedType", {"name": type_name})


@mcp.tool()
def get_xrefs_to(address: str) -> list:
    """
    Get all cross references (code and data) to the given address.
    Address can be hex (e.g., 0x401000) or decimal.
    """
    return safe_get("getXrefsTo", {"address": address})


@mcp.tool()
def get_xrefs_to_field(struct_name: str, field_name: str) -> list:
    """
    Get all cross references to a named struct field (member).
    """
    return safe_get("getXrefsToField", {"struct": struct_name, "field": field_name})


@mcp.tool()
def get_xrefs_to_struct(struct_name: str) -> list:
    """
    Get cross references/usages related to a struct name.
    """
    return safe_get("getXrefsToStruct", {"name": struct_name})


@mcp.tool()
def get_xrefs_to_type(type_name: str) -> list:
    """
    Get xrefs/usages related to a struct or type name.
    Includes global instances, code refs to those, HLIL matches, and functions whose signature mentions the type.
    """
    return safe_get("getXrefsToType", {"name": type_name})


@mcp.tool()
def get_xrefs_to_enum(enum_name: str) -> list:
    """
    Get usages/xrefs of an enum by scanning for member values and matches.
    """
    return safe_get("getXrefsToEnum", {"name": enum_name})


@mcp.tool()
def get_xrefs_to_union(union_name: str) -> list:
    """
    Get cross references/usages related to a union type by name.
    """
    return safe_get("getXrefsToUnion", {"name": union_name})


@mcp.tool()
def get_stack_frame_vars(function_identifier: str) -> list:
    """
    Get stack frame variable information for a function by name or address.
    Returns names, offsets, sizes, and types of local variables.
    """
    ident = (function_identifier or "").strip()
    params = {}
    # Choose param name based on identifier format
    if ident.lower().startswith("0x") or ident.isdigit():
        params["address"] = ident
    else:
        params["name"] = ident
    data = get_json("getStackFrameVars", params)
    if not data:
        return []
    if isinstance(data, dict) and data.get("error"):
        return []
    if isinstance(data, dict) and data.get("stack_frame_vars"):
        return data["stack_frame_vars"]
    return []


@mcp.tool()
def format_value(address: str, text: str, size: int = 0) -> list:
    """
    Convert and annotate a value at an address in Binary Ninja.
    Adds a comment with hex/dec and C literal/string so you can see the change.
    """
    return safe_get("formatValue", {"address": address, "text": text, "size": size}, timeout=None)


@mcp.tool()
def convert_number(text: str, size: int = 0) -> str:
    """
    Convert a number or string to multiple representations (hex/dec/bin, LE/BE, C char/string literals).
    Accepts decimal (e.g., 123), hex (0x7b or 7Bh), binary (0b1111011), octal (0o173),
    char ('A'), or string ("ABC" with escapes like \x41).
    """
    data = get_json("convertNumber", {"text": text, "size": size}, timeout=None)
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    import json as _json

    return _json.dumps(data, indent=2, ensure_ascii=False)


@mcp.tool()
def get_type_info(type_name: str) -> str:
    """
    Resolve a type name and return its declaration and details (kind, members, enum values).
    """
    data = get_json("getTypeInfo", {"name": type_name}, timeout=None)
    if not data:
        return "Error: no response"
    if "error" in data:
        return f"Error: {data.get('error')}"
    import json as _json

    return _json.dumps(data, indent=2, ensure_ascii=False)


@mcp.tool()
def set_function_prototype(name_or_address: str, prototype: str) -> str:
    """
    Set a function's prototype by name or address.
    """
    # Use GET like other endpoints (server accepts complex prototypes)
    ident = (name_or_address or "").strip()
    params = {"prototype": prototype}
    # Choose param name based on identifier format
    if ident.lower().startswith("0x") or ident.isdigit():
        params["address"] = ident
    else:
        params["name"] = ident
    data = get_json("setFunctionPrototype", params)
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and "status" in data:
        return f"Applied prototype at {data.get('address')}: {data.get('applied_type')}"
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    return str(data)


@mcp.tool()
def get_function_signature(function_identifier: str) -> str:
    """
    Get the full signature/prototype of a function by name or address.
    Returns JSON with name, address, return_type, calling_convention, parameters, prototype.
    """
    ident = (function_identifier or "").strip()
    params = {}
    if ident.lower().startswith("0x") or ident.isdigit():
        params["address"] = ident
    else:
        params["name"] = ident
    data = get_json("getFunctionSignature", params, timeout=None)
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    import json as _json

    return _json.dumps(data, indent=2, ensure_ascii=False)


@mcp.tool()
def export_analysis(
    include_prototypes: bool = True,
    include_types: bool = True,
    include_data_types: bool = True,
    include_comments: bool = True,
) -> str:
    """
    Export all user analysis from the active binary.
    Returns JSON with functions (name, address, prototype, parameters),
    types (structs, enums with members), data_labels (address, name, type),
    and comments (address + function comments).
    Each section can be toggled off to reduce output size.
    """
    params = {}
    if not include_prototypes:
        params["prototypes"] = "false"
    if not include_types:
        params["types"] = "false"
    if not include_data_types:
        params["dataTypes"] = "false"
    if not include_comments:
        params["comments"] = "false"
    data = get_json("exportAnalysis", params, timeout=None)
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    import json as _json

    return _json.dumps(data, indent=2, ensure_ascii=False)


@mcp.tool()
def make_function_at(address: str, platform: str = "") -> str:
    """
    Create a function at the given address. Platform is optional (e.g., "linux-x86_64").
    Use "default" to explicitly select the BinaryView/platform default.
    Returns status and function info; no-op if the function already exists.
    """
    params = {"address": address}
    if platform:
        params["platform"] = platform
    data = get_json("makeFunctionAt", params)
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        import json as _json

        return _json.dumps(data, indent=2, ensure_ascii=False)
    if isinstance(data, dict) and data.get("status") == "exists":
        return f"Function already exists at {data.get('address')}: {data.get('name')}"
    if isinstance(data, dict) and data.get("status") == "ok":
        return f"Created function at {data.get('address')}: {data.get('name')}"
    return str(data)


@mcp.tool()
def list_platforms() -> str:
    """
    List all available platform names from Binary Ninja.
    """
    data = get_json("platforms")
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        import json as _json

        return _json.dumps(data, indent=2, ensure_ascii=False)
    plats = data.get("platforms") if isinstance(data, dict) else None
    if not plats:
        return "(no platforms)"
    return "\n".join(plats)


@mcp.tool()
def declare_c_type(c_declaration: str) -> str:
    """
    Create or update a local type from a C declaration.
    """
    data = get_json("declareCType", {"declaration": c_declaration})
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("defined_types"):
        names = ", ".join(data["defined_types"].keys())
        return f"Declared types ({data.get('count', 0)}): {names}"
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    return str(data)


@mcp.tool()
def set_local_variable_type(function_address: str, variable_name: str, new_type: str) -> str:
    """
    Set a local variable's type.
    """
    data = get_json(
        "setLocalVariableType",
        {
            "functionAddress": function_address,
            "variableName": variable_name,
            "newType": new_type,
        },
    )
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("status") == "ok":
        return f"Retyped {data.get('variable')} in {data.get('function')} to {data.get('applied_type')}"
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    return str(data)


@mcp.tool()
def patch_bytes(address: str, data: str, save_to_file: bool = True) -> str:
    """
    Patch bytes at a given address in the binary.
    - address: Address to patch (hex string like "0x401000" or decimal)
    - data: Hex string of bytes to write (e.g., "90 90" or "9090" or "0x90 0x90")
    - save_to_file: If True (default), save patched binary to disk and re-sign on macOS.
                    If False, only modify in memory without affecting the original file.

    Returns status with original and patched bytes.
    On macOS, automatically re-signs the binary after patching to avoid execution errors.
    """
    # Handle boolean type conversion (MCP may pass as string)
    if isinstance(save_to_file, str):
        save_to_file = save_to_file.lower() not in ("false", "0", "no")

    params = {"address": address, "data": data, "save_to_file": save_to_file}
    result = get_json("patch", params)
    if not result:
        return "Error: no response"

    status = result.get("status") if isinstance(result, dict) else None
    if status in ("ok", "partial"):
        orig = result.get("original_bytes", "")
        patched = result.get("patched_bytes", "")
        written = result.get("bytes_written", 0)
        requested = result.get("bytes_requested", 0)
        addr = result.get("address", address)
        saved = result.get("saved_to_file", False)
        saved_path = result.get("saved_path", "")
        save_error = result.get("save_error", "")
        codesign = result.get("codesign", {})
        warning = result.get("warning", "")

        msg = f"Patched {written}/{requested} bytes at {addr}"
        if status == "partial":
            msg += " (PARTIAL WRITE)"
        if warning:
            msg += f"\nWarning: {warning}"
        if orig:
            msg += f"\nOriginal: {orig}"
        if patched:
            msg += f"\nPatched:  {patched}"
        if saved:
            msg += f"\nSaved to file: {saved_path}"
        elif save_error:
            msg += f"\nWarning: File not saved - {save_error}"

        # Show codesign status for macOS
        if codesign:
            if codesign.get("success"):
                msg += f"\nCode signing: {codesign.get('message', 'Re-signed successfully')}"
            elif codesign.get("attempted"):
                msg += f"\nCode signing: Failed - {codesign.get('error', 'Unknown error')}"

        return msg
    if isinstance(result, dict) and "error" in result:
        return f"Error: {result['error']}"
    return str(result)


@mcp.tool()
def wowemulation_scan_lua_api_strings(
    namespace_filter: str = "", offset: int = 0, limit: int = 100
) -> str:
    """
    Scan for Lua API 'Usage:' strings and find their associated native function pointers.
    Useful for WoW binary analysis to map Lua API names to C function addresses.

    Args:
        namespace_filter: Optional filter string (e.g. 'Unit' to find UnitName, UnitLevel, etc.)
        offset: Pagination offset
        limit: Maximum entries to return
    """
    data = get_json(
        "wowemulation/scanLuaApi",
        {"filter": namespace_filter, "offset": offset, "limit": limit},
        timeout=30,
    )
    if not data:
        return "Error: no response from server"
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    entries = data.get("entries", [])
    total = data.get("total", 0)
    lines = [f"Lua API strings: {len(entries)} shown, {total} total"]
    for e in entries:
        func_info = (
            f" -> {e.get('func_name', '?')} @ {e.get('func_addr', '?')}"
            if e.get("func_addr")
            else ""
        )
        lines.append(f"  {e.get('string_addr', '?')}: {e.get('string', '?')}{func_info}")
    return "\n".join(lines)


@mcp.tool()
def wowemulation_scan_rtti_entries(
    class_filter: str = "", offset: int = 0, limit: int = 100
) -> str:
    """
    Scan for MSVC RTTI type_info entries and their associated vtables.
    Returns class names, type_info addresses, vtable addresses, and virtual function counts.

    Args:
        class_filter: Optional filter string (e.g. 'CUnit' to find CUnit-related classes)
        offset: Pagination offset
        limit: Maximum entries to return
    """
    data = get_json(
        "wowemulation/scanRtti",
        {"filter": class_filter, "offset": offset, "limit": limit},
        timeout=30,
    )
    if not data:
        return "Error: no response from server"
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    entries = data.get("entries", [])
    total = data.get("total", 0)
    lines = [f"RTTI entries: {len(entries)} shown, {total} total"]
    for e in entries:
        vtable = e.get("vtable_addr", "none")
        vfuncs = e.get("num_vfuncs", 0)
        lines.append(
            f"  {e.get('class_name', '?')} @ ti={e.get('type_info_addr', '?')} vt={vtable} ({vfuncs} vfuncs)"
        )
    return "\n".join(lines)


@mcp.tool()
def wowemulation_scan_update_fields(
    object_type: str = "", offset: int = 0, limit: int = 100
) -> str:
    """
    Scan for WoW update field strings (CG*Data:: patterns) and their handler functions.
    Useful for mapping update field names to their accessor/mutator functions.

    Args:
        object_type: Optional filter (e.g. 'CGUnitData', 'CGPlayerData')
        offset: Pagination offset
        limit: Maximum entries to return
    """
    data = get_json(
        "wowemulation/scanUpdateFields",
        {"filter": object_type, "offset": offset, "limit": limit},
        timeout=30,
    )
    if not data:
        return "Error: no response from server"
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    entries = data.get("entries", [])
    total = data.get("total", 0)
    lines = [f"Update fields: {len(entries)} shown, {total} total"]
    for e in entries:
        handler = (
            f" -> {e.get('handler_name', '?')} @ {e.get('handler_addr', '?')}"
            if e.get("handler_addr")
            else ""
        )
        lines.append(f"  {e.get('string_addr', '?')}: {e.get('field_name', '?')}{handler}")
    return "\n".join(lines)


@mcp.tool()
def wowemulation_batch_rename_functions(renames: str) -> str:
    """
    Rename multiple functions in one MCP call. Accepts a JSON-encoded list of
    {address, name} pairs.

    Args:
        renames: JSON string like [{"address": "0x401000", "name": "MyFunc"}, ...]
    """
    import json as _json

    try:
        renames_list = _json.loads(renames)
    except (_json.JSONDecodeError, ValueError) as e:
        return f"Error: Invalid JSON - {e}"

    if not isinstance(renames_list, list):
        return "Error: renames must be a JSON array"

    try:
        response = _do_post(
            f"{binja_server_url}/wowemulation/batchRename",
            30,
            json={"renames": renames_list},
        )
        response.encoding = "utf-8"
        data = response.json()
    except Exception as e:
        return f"Error: request failed - {e}"

    if isinstance(data, dict) and "error" in data and "success_count" not in data:
        return f"Error: {data['error']}"

    success = data.get("success_count", 0)
    failure = data.get("failure_count", 0)
    total = data.get("total", 0)
    msg = f"Renamed {success}/{total} functions ({failure} failures)"

    # Show failures if any
    for r in data.get("results", []):
        if r.get("status") == "error":
            msg += f"\n  FAIL {r.get('address', '?')}: {r.get('error', '?')}"

    return msg


@mcp.tool()
def wowemulation_discover_lua_tables(
    min_entries: int = 3, offset: int = 0, limit: int = 50
) -> str:
    """
    Discover luaL_Reg-style registration tables by scanning .rdata/.data sections.
    Finds contiguous arrays of {string_ptr, func_ptr} pairs and validates string content.
    Returns discovered tables with method names and function addresses.

    Args:
        min_entries: Minimum entries for a run to count as a table (default 3)
        offset: Pagination offset (on tables list)
        limit: Maximum tables to return (default 50)
    """
    data = get_json(
        "wowemulation/discoverLuaTables",
        {"min_entries": min_entries, "offset": offset, "limit": limit},
        timeout=60,
    )
    if not data:
        return "Error: no response from server"
    if isinstance(data, dict) and "error" in data and "tables" not in data:
        return f"Error: {data['error']}"

    tables = data.get("tables", [])
    total = data.get("total", 0)
    total_entries = data.get("total_entries", 0)
    lines = [f"Lua registration tables: {len(tables)} shown, {total} total, {total_entries} total entries"]
    for t in tables:
        lines.append(
            f"\n  Table @ {t.get('table_addr', '?')} ({t.get('entry_count', 0)} entries, "
            f"section={t.get('section', '?')}, valid={t.get('valid_names', 0)})"
        )
        for e in t.get("entries", []):
            name = e.get("name") or "<invalid>"
            lines.append(f"    {name} -> {e.get('func_addr', '?')}")
    return "\n".join(lines)


@mcp.tool()
def wowemulation_walk_rtti_vtables(
    class_filter: str = "", offset: int = 0, limit: int = 100
) -> str:
    """
    Walk RTTI vtables by scanning .rdata for COL structures via raw RVA matching.
    Bypasses broken xrefs. Returns class names, vtable addresses, and vfunc counts.

    Args:
        class_filter: Optional substring filter on class name
        offset: Pagination offset
        limit: Maximum entries to return
    """
    data = get_json(
        "wowemulation/walkRttiVtables",
        {"filter": class_filter, "offset": offset, "limit": limit},
        timeout=120,
    )
    if not data:
        return "Error: no response from server"
    if isinstance(data, dict) and "error" in data and "entries" not in data:
        return f"Error: {data['error']}"

    entries = data.get("entries", [])
    total = data.get("total", 0)
    lines = [f"RTTI vtables: {len(entries)} shown, {total} total"]
    for e in entries:
        vtable = e.get("vtable_addr") or "none"
        vfuncs = e.get("num_vfuncs", 0)
        first = ", ".join(e.get("first_vfuncs", [])[:3])
        suffix = f" [{first}]" if first else ""
        lines.append(
            f"  {e.get('class_name', '?')} ti={e.get('type_info_addr', '?')} "
            f"vt={vtable} ({vfuncs} vfuncs){suffix}"
        )
    return "\n".join(lines)


@mcp.tool()
def wowemulation_scan_vtables(
    min_methods: int = 2, offset: int = 0, limit: int = 100
) -> str:
    """
    Scan .rdata and .data for vtable-like structures: contiguous runs of .text
    pointers. Resolves class names via RTTI COL when present.

    Complements walk_rtti_vtables by finding vtables that lack RTTI structures
    (common in Blizzard game binaries where RTTI is stripped for game classes).

    Args:
        min_methods: Minimum number of virtual methods to report (default 2)
        offset: Pagination offset
        limit: Maximum entries to return
    """
    data = get_json(
        "wowemulation/scanVtables",
        {"min_methods": min_methods, "offset": offset, "limit": limit},
        timeout=60,
    )
    if not data:
        return "Error: no response from server"
    if isinstance(data, dict) and "error" in data and "vtables" not in data:
        return f"Error: {data['error']}"

    vtables = data.get("vtables", [])
    total = data.get("total", 0)
    named_count = data.get("named_count", 0)
    lines = [
        f"Vtable scan: {len(vtables)} shown, {total} total "
        f"({named_count} named, {total - named_count} unnamed)"
    ]
    for v in vtables:
        addr = v.get("vtable_addr", "?")
        n = v.get("num_methods", 0)
        sec = v.get("section", "?")
        name = v.get("class_name")
        first = ", ".join(v.get("first_methods", [])[:3])
        suffix = f" [{first}]" if first else ""
        if name:
            ti = v.get("type_info_addr", "?")
            lines.append(f"  {name} vt={addr} ({n} methods, {sec}) ti={ti}{suffix}")
        else:
            lines.append(f"  <unnamed> vt={addr} ({n} methods, {sec}){suffix}")
    return "\n".join(lines)


@mcp.tool()
def wowemulation_batch_label_data(labels: str) -> str:
    """
    Name data variables at specified addresses. Accepts a JSON-encoded list of
    {address, name, size?} objects.

    Args:
        labels: JSON string like [{"address": "0x141f6b230", "name": "g_luaReg_Frame", "size": 8}, ...]
    """
    import json as _json

    try:
        labels_list = _json.loads(labels)
    except (_json.JSONDecodeError, ValueError) as e:
        return f"Error: Invalid JSON - {e}"

    if not isinstance(labels_list, list):
        return "Error: labels must be a JSON array"

    try:
        response = _do_post(
            f"{binja_server_url}/wowemulation/batchLabelData",
            30,
            json={"labels": labels_list},
        )
        response.encoding = "utf-8"
        data = response.json()
    except Exception as e:
        return f"Error: request failed - {e}"

    if isinstance(data, dict) and "error" in data and "success_count" not in data:
        return f"Error: {data['error']}"

    success = data.get("success_count", 0)
    failure = data.get("failure_count", 0)
    total = data.get("total", 0)
    msg = f"Labeled {success}/{total} data variables ({failure} failures)"

    for r in data.get("results", []):
        if r.get("status") == "error":
            msg += f"\n  FAIL {r.get('address', '?')}: {r.get('error', '?')}"

    return msg


@mcp.tool()
def wowemulation_batch_create_functions(entries: str) -> str:
    """
    Create function entries at addresses that lack them. Accepts a JSON-encoded list of
    {address, name?} objects. If a function already exists, it will be renamed if name is provided.

    Args:
        entries: JSON string like [{"address": "0x140001000", "name": "MyFunc"}, ...]
    """
    import json as _json

    try:
        entries_list = _json.loads(entries)
    except (_json.JSONDecodeError, ValueError) as e:
        return f"Error: Invalid JSON - {e}"

    if not isinstance(entries_list, list):
        return "Error: entries must be a JSON array"

    try:
        response = _do_post(
            f"{binja_server_url}/wowemulation/batchCreateFunctions",
            30,
            json={"entries": entries_list},
        )
        response.encoding = "utf-8"
        data = response.json()
    except Exception as e:
        return f"Error: request failed - {e}"

    if isinstance(data, dict) and "error" in data and "success_count" not in data:
        return f"Error: {data['error']}"

    success = data.get("success_count", 0)
    failure = data.get("failure_count", 0)
    total = data.get("total", 0)
    msg = f"Created/updated {success}/{total} functions ({failure} failures)"

    for r in data.get("results", []):
        if r.get("status") == "error":
            msg += f"\n  FAIL {r.get('address', '?')}: {r.get('error', '?')}"

    return msg


@mcp.tool()
def wowemulation_scan_lea_operands(
    category: str = "", offset: int = 0, limit: int = 100
) -> str:
    """
    Scan .text for instructions referencing known string addresses. Bypasses broken
    xref engines by iterating target string addresses and checking code/data refs.

    Categories: lua_api, update_field, error_enum, rtti, source_path

    Args:
        category: Filter to one category (empty = all)
        offset: Pagination offset
        limit: Maximum entries to return
    """
    try:
        url = _build_url("wowemulation/scanLeaOperands", {"category": category, "offset": offset, "limit": limit})
        response = _do_get(url, 120)
        response.encoding = "utf-8"
        data = response.json()
    except Exception as e:
        return f"Error: request failed - {e}"

    if isinstance(data, dict) and "error" in data and "total" not in data:
        return f"Error: {data['error']}"

    total = data.get("total", 0)
    target_count = data.get("target_count", 0)
    cat_breakdown = data.get("category_breakdown", {})

    lines = [f"LEA operand scan: {total} references found across {target_count} target strings"]
    if cat_breakdown:
        lines.append("Category breakdown:")
        for cat, count in sorted(cat_breakdown.items()):
            lines.append(f"  {cat}: {count}")

    entries = data.get("entries", [])
    lines.append(f"\nShowing {len(entries)} entries (offset={offset}):")
    for e in entries:
        func_name = e.get("func_name", "(unknown)")
        func_addr = e.get("func_addr", "?")
        cat = e.get("category", "?")
        val = e.get("value", "")
        lines.append(f"  {func_addr} {func_name} [{cat}] {val[:120]}")

    return "\n".join(lines)


@mcp.tool()
def wowemulation_propagate_symbols(
    mode: str = "export",
    hash_file: str = "",
    hash_mode: str = "mnemonic",
    offset: int = 0,
    limit: int = 5000,
) -> str:
    """
    Cross-version symbol propagation via function-level hashing.

    Two hash modes:
      mnemonic - SHA-256 of size + mnemonics + count (same-architecture)
      semantic - SHA-256 of arch-independent properties (cross-architecture)

    Two operation modes:
      export - Export hashes of all named functions
      import - Import hashes from a file and rename matching unnamed functions

    Args:
        mode: 'export' or 'import'
        hash_file: Path to hash file (required for import, optional for export)
        hash_mode: 'mnemonic' or 'semantic' (export mode only)
        offset: Pagination offset (export mode only)
        limit: Maximum entries to return (export mode only)
    """
    if mode == "export":
        try:
            url = _build_url("wowemulation/propagateSymbols", {
                "mode": "export",
                "hash_mode": hash_mode,
                "offset": offset,
                "limit": limit,
            })
            response = _do_get(url, 300)
            response.encoding = "utf-8"
            data = response.json()
        except Exception as e:
            return f"Error: request failed - {e}"

        if isinstance(data, dict) and "error" in data and "entries" not in data:
            return f"Error: {data['error']}"

        total = data.get("total", 0)
        entries = data.get("entries", [])

        # Write to disk if hash_file is provided
        if hash_file:
            try:
                import os
                os.makedirs(os.path.dirname(os.path.abspath(hash_file)), exist_ok=True)
                with open(hash_file, "w") as f:
                    f.write(f"# Exported {len(entries)}/{total} function hashes\n")
                    f.write(f"# hash_mode: {hash_mode}\n")
                    f.write(f"# Format: hash address name size\n")
                    for e in entries:
                        f.write(f"{e['hash']} {e['address']} {e['name']} {e['size']}\n")
                file_msg = f"Wrote {len(entries)} entries to {hash_file}"
            except Exception as write_err:
                file_msg = f"Failed to write hash file: {write_err}"
        else:
            file_msg = None

        lines = [f"Exported {len(entries)}/{total} function hashes"]
        if file_msg:
            lines.append(file_msg)
        for e in entries[:20]:
            lines.append(f"  {e['hash'][:16]}... {e['address']} {e['name']} (size={e['size']})")
        if len(entries) > 20:
            lines.append(f"  ... and {len(entries) - 20} more")

        return "\n".join(lines)

    elif mode == "import":
        if not hash_file:
            return "Error: hash_file path required for import mode"

        # Read hash file
        source_hashes = []
        try:
            with open(hash_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split()
                    if len(parts) >= 4:
                        source_hashes.append({
                            "hash": parts[0],
                            "address": parts[1],
                            "name": parts[2],
                            "size": int(parts[3]),
                        })
        except Exception as e:
            return f"Error reading hash file: {e}"

        if not source_hashes:
            return "Error: no valid hashes found in file"

        try:
            response = _do_post(
                f"{binja_server_url}/wowemulation/propagateSymbols",
                600,
                json={"mode": "import", "source_hashes": source_hashes},
            )
            response.encoding = "utf-8"
            data = response.json()
        except Exception as e:
            return f"Error: request failed - {e}"

        if isinstance(data, dict) and "error" in data:
            return f"Error: {data['error']}"

        matched = data.get("matched", 0)
        total = data.get("total_functions", 0)
        collisions = data.get("collisions", 0)

        return (
            f"Propagation results:\n"
            f"  Matched: {matched}\n"
            f"  Total unnamed functions: {total}\n"
            f"  Hash collisions (size mismatch): {collisions}\n"
            f"  Source hashes loaded: {len(source_hashes)}"
        )

    else:
        return f"Unknown mode '{mode}'. Use 'export' or 'import'."


if __name__ == "__main__":
    # Important: write any logs to stderr to avoid corrupting MCP stdio JSON-RPC
    print("Starting MCP bridge service...", file=_sys.stderr)
    try:
        mcp.run()
    except Exception as _e:
        # Ensure any runtime exception is captured in the log file
        _bridge_excepthook(type(_e), _e, _e.__traceback__)
        raise
