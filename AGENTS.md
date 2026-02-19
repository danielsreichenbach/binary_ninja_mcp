# AGENTS.md

This repository contains a Binary Ninja plugin and MCP (Model Context Protocol) bridge that enables AI-assisted binary analysis. The plugin exposes Binary Ninja's capabilities through HTTP endpoints, and the bridge connects MCP clients to the plugin.

## Project Structure

### Core Components

- **plugin/**: Binary Ninja plugin (runs inside Binary Ninja)
  - `plugin/__init__.py`: Plugin entry point, UI integration, menu commands, and autostart logic
  - `plugin/api/endpoints.py`: HTTP endpoint handlers that expose Binary Ninja operations
  - `plugin/server/http_server.py`: HTTP server handling requests from the bridge
  - `plugin/core/binary_operations.py`: Core operations for decompilation, analysis, renaming, patching
  - `plugin/core/config.py`: Configuration using dataclasses (server host/port, logging)
  - `plugin/utils/`: Utility modules (string escaping, number conversion, auto-setup)

- **bridge/**: MCP bridge (runs standalone, communicates with plugin)
  - `bridge/binja_mcp_bridge.py`: FastMCP server with 80+ tools for binary analysis
  - `bridge/requirements.txt`: Bridge dependencies

- **scripts/**: Setup and installation utilities
- **example/**: Test files for verification
  - `chal.c`: Example CTF challenge source code
  - `chal`: Compiled binary (ARM64 Mach-O) for testing
  - `mcp_client_installer.py`: Auto-installs bridge entries in supported MCP clients
  - `setup_claude_desktop.py`: Claude Desktop-specific setup

### Multi-Binary Support

The plugin maintains a registry of open BinaryViews using weak references, allowing analysis of multiple binaries simultaneously. Use `/binaries` to list and `/selectBinary` to switch between them.

## Development Commands

### Code Quality

**Ruff** is used for linting and formatting (configured in `ruff.toml`):

```bash
# Check for issues
ruff check .

# Auto-fix issues
ruff check --fix .

# Check formatting
ruff format --check .

# Format code
ruff format .
```

### Testing

This project has **no automated tests**. To test changes:
1. Use the example CTF challenge in `example/` directory
2. Load `example/chal` in Binary Ninja
3. Start the bridge: `python bridge/binja_mcp_bridge.py`
4. Test MCP tools through your MCP client
5. Check Binary Ninja logs for errors

### Installation

The plugin auto-registers with MCP clients on first load. To test changes:
1. Copy the plugin directory to Binary Ninja's plugins folder
2. Restart Binary Ninja
3. Start/stop server via menu: `Plugins > MCP Server > Start/Stop MCP Server`
4. Status indicator in Binary Ninja's status bar shows server state

### Package Management

This project uses **uv** for Python package management:
- `pyproject.toml`: Project configuration with `[dependency-groups]`
- `uv.lock`: Locked dependency versions (similar to package-lock.json)
- `requirements.txt`: Standalone bridge dependencies for non-uv environments

To sync dependencies:
```bash
uv sync
```

To add a dependency:
```bash
uv add <package>           # Runtime dependency
uv add --group dev <package>  # Development dependency
```

### Installation

The plugin auto-registers with MCP clients on first load. To test changes:
1. Copy the plugin directory to Binary Ninja's plugins folder
2. Restart Binary Ninja
3. Start/stop server via menu: `Plugins > MCP Server > Start/Stop MCP Server`
4. Status indicator in Binary Ninja's status bar shows server state

### Running the Bridge Standalone

```bash
cd bridge
python binja_mcp_bridge.py
```

The bridge connects to `http://localhost:9009` (configurable via `binja_server_url`).

## Code Conventions

### Python Version

Requires Python 3.12+ (specified in `pyproject.toml` and `plugin.json`).

### Type Hints

Type hints are used consistently:
- `str | None` for optional strings (Python 3.10+ syntax)
- `dict[str, Any]` for generic dictionaries
- `list[dict[str, Any]]` for lists of dicts
- `dict[str, str]` for string-keyed, string-valued dictionaries

### Error Handling

Errors are logged to Binary Ninja's logger:
- `bn.log_info()`: General info
- `bn.log_warn()`: Warnings
- `bn.log_error()`: Errors
- `bn.log_debug()`: Debug messages

Public functions raise exceptions that HTTP endpoints catch and convert to JSON error responses:
- `RuntimeError("No binary loaded")`: When no BinaryView is active
- `ValueError("...")`: For invalid inputs or operations that can't be performed
- Specific descriptive messages are preferred over generic errors

### Naming Conventions

- Functions: `snake_case`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`
- Private members: `_prefix`
- Configuration settings in JSON: `lowerCamelCase`

### String Handling

Binary Ninja symbol names may contain non-ASCII characters. Use `escape_non_ascii()` from `plugin/utils/string_utils.py` to escape them for display or JSON serialization.

### Number Parsing

Hex addresses can be passed as strings like `"0x401000"` or decimal integers. Use helper functions:
- `parse_int_or_default(val, default)`: Parse int or return default
- Convert between bases using `convert_number()` utility when needed

### HTTP Response Format

All HTTP endpoints return JSON with consistent structure:

Success:
```json
{
  "status": "ok",
  "data": { ... }
}
```

Error:
```json
{
  "error": "Descriptive error message",
  "details": { ... }
}
```

### MCP Tool Conventions

MCP tools in the bridge (`bridge/binja_mcp_bridge.py`) follow these patterns:
- `@mcp.tool()` decorator marks tool functions
- Function names use `snake_case` matching the tool name (e.g., `decompile_function`)
- Include docstrings with descriptions (exposed to LLM clients via MCP)
- Use helper functions `get_json()`, `get_text()`, `safe_get()`, `safe_post()` for server communication
- Return types:
  - `list[str]`: For paginated listings (functions, strings, etc.)
  - `str`: For single-operation results or error messages
  - Empty list or error string for failures

### Multi-Variable Renaming

Batch renaming supports three input formats (via `rename_multi_variables`):
1. `renames_json`: JSON array of `{old, new}` objects
2. `mapping_json`: JSON object mapping `old->new`
3. `pairs`: Compact string `"old1:new1,old2:new2"`

Later entries can refer to names produced by earlier renames. Renames apply in order.

## Important Gotchas

### BinaryView Lifecycle

- BinaryViews are stored as weak references and auto-pruned when closed
- Always check `if binary_ops.current_view` before operations
- The server tracks open views via UI notifications and timers; closed views are removed automatically
- After closing a binary, use `/binaries` to verify the registry reflects current state

### Function Identification

Functions can be identified by:
- Name (string): `"main"`, `"sub_401000"`
- Address (hex string): `"0x401000"`
- Address (decimal): `4198400`

Helper `get_function_by_name_or_address()` resolves both forms.

### Type Parsing

Binary Ninja's type parser accepts multiple forms:
- Full C declarations: `int __cdecl foo(int a, char *b)`
- Bare types: `int(int, char*)`
- Named types: `struct MyStruct`, `enum MyEnum`

Always wrap parsing in try-except and provide helpful error messages.

### Patching Bytes

The `patch_bytes` endpoint accepts data in multiple formats:
- Hex string: `"90 90"`, `"9090"`, or `"0x90 0x90"`
- List of integers: `[0x90, 0x90]`
- Bytes object: `b"\x90\x90"`

On macOS, patched binaries are automatically re-signed. Use `save_to_file=False` for in-memory only.

### WoW Emulation Tools

Specialized tools for WoW binary analysis (in `BinaryNinjaEndpoints` and bridge):
- `wowemulation_scan_lua_api_strings`: Map Lua API names to C function pointers
- `wowemulation_scan_rtti_entries`: Find RTTI type_info and vtables
- `wowemulation_scan_update_fields`: Map update field strings to handlers
- `wowemulation_batch_rename_functions`: Bulk rename functions with JSON list

These are paginated with `offset` and `limit` parameters.

### UI Integration

The plugin integrates with Binary Ninja's UI:
- Menu commands: `Plugins > MCP Server > Start/Stop MCP Server`
- Status indicator: Toggle button in status bar (green = running, red = stopped)
- Auto-start on file open (unless manually stopped)
- Settings: `mcp.renamePrefix` (default: `mcp_`), `mcp.showStatusButton`

UI operations run on the main thread via `binaryninjaui.execute_on_main_thread()`.

### Bridge Exception Handling

The bridge installs an early excepthook (`_bridge_excepthook`) to capture ImportErrors at module load time. Logs go to stderr to avoid corrupting MCP stdio JSON-RPC.

### Address Formatting

Always return addresses as hex strings (e.g., `"0x401000"`) in JSON responses. Binary Ninja may return integers; convert them for consistency.

### Pagination

List endpoints support `offset` and `limit` parameters:
- `offset`: Starting index (0-based)
- `limit`: Maximum items to return
- Example: `/strings?offset=100&limit=50`

Non-paginated variants (`list_all_strings`) aggregate all pages but may be slow for large binaries.

## MCP Tools Summary

The bridge exposes 80+ tools grouped by category:

### Listing & Discovery
- `list_methods`, `list_imports`, `list_exports`, `list_namespaces`, `list_classes`, `list_sections`, `list_segments`, `list_data_items`, `list_local_types`, `list_strings`, `list_all_strings`, `list_binaries`
- `search_functions_by_name`, `search_types`, `list_strings_filter`
- `get_entry_points`, `get_binary_status`

### Function Analysis
- `decompile_function`, `get_il` (hlil/mlil/llil with SSA), `fetch_disassembly`, `function_at`

### Types & Structures
- `define_types`, `declare_c_type`, `get_user_defined_type`, `get_type_info`

### Variables & Renaming
- `rename_function`, `rename_data`, `rename_single_variable`, `rename_multi_variables`, `retype_variable`, `set_local_variable_type`
- `get_stack_frame_vars`, `set_function_prototype`

### Comments
- `set_comment`, `get_comment`, `delete_comment`, `set_function_comment`, `get_function_comment`, `delete_function_comment`

### Cross References
- `get_xrefs_to`, `get_xrefs_to_struct`, `get_xrefs_to_field`, `get_xrefs_to_type`, `get_xrefs_to_enum`, `get_xrefs_to_union`

### Data & Memory
- `hexdump_address`, `hexdump_data`, `get_data_decl`, `list_data_items`

### Modification
- `patch_bytes`, `make_function_at`

### Number Conversion
- `convert_number`, `format_value` (adds comment with conversions)

### WoW-Specific
- `wowemulation_scan_lua_api_strings`, `wowemulation_scan_rtti_entries`, `wowemulation_scan_update_fields`, `wowemulation_batch_rename_functions`

### Binary Management
- `list_binaries`, `select_binary`

### Platform Info
- `list_platforms`

## Configuration

### Server Config (`ServerConfig` dataclass)
- `host`: HTTP server host (default: `"localhost"`)
- `port`: HTTP server port (default: `9009`)

### Binary Ninja Settings
Registered in Binary Ninja's settings system:
- `mcp.renamePrefix`: Prefix for renamed functions/variables (default: `"mcp_"`)
- `mcp.showStatusButton`: Show status indicator in status bar (default: `true`)

## Binary Ninja API Reference

The Binary Ninja Python API documentation is at <https://api.binary.ninja/>.

Key modules used by this plugin:

- [binaryninja.binaryview](https://api.binary.ninja/binaryninja.binaryview-module.html) - BinaryView, data vars, code/data refs
- [binaryninja.function](https://api.binary.ninja/binaryninja.function-module.html) - Function objects, parameters, HLIL/MLIL
- [binaryninja.types](https://api.binary.ninja/binaryninja.types-module.html) - Type creation, parsing, StructureBuilder
- [binaryninja.demangle](https://api.binary.ninja/binaryninja.demangle-module.html) - Name demangling
- [binaryninja.plugin](https://api.binary.ninja/binaryninja.plugin-module.html) - Plugin registration, background tasks

## Dependencies

### Plugin
- `binaryninja`: Binary Ninja Python API (provided by Binary Ninja installation)
- No external runtime dependencies (uses stdlib for HTTP server)

### Bridge
- `mcp>=1.0`: MCP protocol implementation
- `requests>=2.28`: HTTP client for communicating with plugin
- Dependencies managed via `uv` (see `uv.lock` and `pyproject.toml`)

### Dev Dependencies
- `ruff>=0.8`: Linting and formatting
- `pyright>=1.1`: Type checking (configured in `pyproject.toml` via `[dependency-groups]`)
- `ruff>=0.8`: Linting and formatting
- `pyright>=1.1`: Type checking (configured in `pyproject.toml`)

## CI/CD

GitHub Actions (`.github/workflows/lint-format.yml`) runs on:
- Push to `main`
- Pull requests to `main`

Workflow:
1. Checks out code
2. Sets up Python 3.12
3. Installs Ruff
4. Runs `ruff check .` (fails on errors)
5. Runs `ruff format --check .` (fails on formatting issues)
