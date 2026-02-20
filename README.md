# Binary Ninja MCP

This repository contains a Binary Ninja plugin, MCP server, and bridge that enables seamless integration of Binary Ninja's capabilities with your favorite LLM client.

![Binary Ninja MCP Logo](images/logo-small.png)

## Features

- Seamless, real-time integration between Binary Ninja and MCP clients
- Enhanced reverse engineering workflow with AI assistance
- Support for every MCP client (Cline, Claude desktop, Roo Code, etc.)
- Open multiple binaries and switch the active target automatically
- 80+ MCP tools for comprehensive binary analysis, decompilation, and modification

## Examples

### Solving a CTF Challenge

Check out [this demo video on YouTube](https://www.youtube.com/watch?v=0ffMHH39L_M) that uses the extension to solve a CTF challenge.

## Architecture

```
+-------------------+     HTTP (localhost:9009)     +------------------+
|  Binary Ninja  | <---------------------------> |   Plugin       | MCP Protocol   |   Bridge       |
|     (GUI)     |                             |  (HTTP Server) | <------------> |  (FastMCP)    |
+-------------------+                             +------------------+                 +------------------+
        |                                                                          |
        +--------------------------------------------------------------------------+
        |
        v
+------------------+
|  MCP Client    | (Cline, Claude Desktop, etc.)
+------------------+
```

1. **Plugin** (runs inside Binary Ninja): Loads binaries, performs analysis via Binary Ninja API
2. **HTTP Server** (inside plugin): Exposes Binary Ninja operations as REST endpoints
3. **Bridge** (standalone process): Implements MCP protocol, translates tool calls to HTTP requests
4. **MCP Client**: Your LLM client (Claude, Cline, etc.) connects to the bridge

## Quick Start

### 1. Install Prerequisites

- [Binary Ninja](https://binary.ninja/) (version 4000+)
- Python 3.12+
- An MCP client (see list below)

### 2. Install MCP Client

We recommend installing the MCP client before installing Binary Ninja MCP for automatic setup. Supported clients:

1. Cline (recommended)
2. Roo Code
3. Claude Desktop (recommended)
4. Cursor
5. Windsurf
6. Claude Code
7. LM Studio

### 3. Install the Extension

#### Option A: Binary Ninja Plugin Manager

1. Open Binary Ninja
2. Go to `Plugins > Manage Plugins`
3. Search for "Binary Ninja MCP"
4. Click Install

![Plugin Manager](images/plugin-manager-listing.png)

#### Option B: Manual Install

Copy this repository into the [Binary Ninja plugins folder](https://docs.binary.ninja/guide/plugins.html).

### 4. Start Using

1. Open Binary Ninja and load a binary
2. Click the MCP Server button in the status bar (bottom-left corner)
3. Start prompting your MCP client about the binary

The status button shows:
- 🟢 **MCP: Running** (green) - Server is active
- 🔴 **MCP: Stopped** (red) - Server is not running

## Multi-Binary Support

You can open multiple binaries in Binary Ninja and switch between them:

```bash
# List all open binaries (returns IDs like 1, 2, 3)
list_binaries()

# Switch to a specific binary by ID
select_binary(view="2")

# Or switch by filename
select_binary(view="my_program.exe")
```

This is useful for analyzing:
- Multiple versions of a binary
- A program and its libraries
- Malware families with multiple samples

## Example Challenge

An example CTF challenge is included in the `example/` directory for testing the plugin:

- `chal` - Compiled Mach-O binary (ARM64)
- `chal.c` - Source code

Load this binary in Binary Ninja to experiment with the MCP tools before analyzing your own targets.

## Usage Examples

### CTF Challenges

```txt
You're the best CTF player in the world. Please solve this reversing CTF challenge in the <folder_name> folder using Binary Ninja. Rename ALL the function and the variables during your analyzation process (except for main function) so I can better read the code. Write a python solve script if you need. Also, if you need to create struct or anything, please go ahead. Reverse the code like a human reverser so that I can read the decompiled code that analyzed by you.
```

### Malware Analysis

```txt
Your task is to analyze an unknown file which is currently open in Binary Ninja. You can use the existing MCP server called "binary_ninja_mcp" to interact with the Binary Ninja instance and retrieve information, using the tools made available by this server. In general use the following strategy:

- Start from the entry point of code
- If this function call others, make sure to follow through the calls and analyze these functions as well to understand their context
- If more details are necessary, disassemble or decompile the function and add comments with your findings
- Inspect the decompilation and add comments with your findings to important areas of code
- Add a comment to each function with a brief summary of what it does
- Rename variables and function parameters to more sensible names
- Change the variable and argument types if necessary (especially pointer and array types)
- Change function names to be more descriptive, using mcp_ as prefix.
- NEVER convert number bases yourself. Use the convert_number MCP tool if needed!
- When you finish your analysis, report how long the analysis took
- At the end, create a report with your findings.
- Based only on these findings, make an assessment on whether the file is malicious or not.
```

### Multi-Binary Analysis Workflow

```txt
I have 3 binaries open in Binary Ninja. Please analyze all of them and compare their string handling functions.

1. First, list all open binaries with list_binaries()
2. For each binary, use select_binary() to switch to it
3. Search for string-related functions using search_functions_by_name()
4. Decompile each function with decompile_function()
5. Compare the implementations and create a summary
```

## Supported Capabilities

The MCP bridge provides 80+ tools organized by category:

### Analysis & Decompilation
- `decompile_function` - Decompile a function with addresses
- `get_il` - Get HLIL, MLIL, or LLIL (with SSA support)
- `fetch_disassembly` - Get assembly for a function
- `get_entry_points` - List entry points

### Listing & Discovery
- `list_methods`, `list_imports`, `list_exports`, `list_namespaces`, `list_classes`
- `list_sections`, `list_segments`, `list_data_items`, `list_local_types`
- `list_strings`, `list_all_strings`, `list_strings_filter`
- `list_binaries`, `get_binary_status`
- `search_functions_by_name`, `search_types`

### Types & Structures
- `define_types` - Add types from C code
- `declare_c_type` - Create/update a single type
- `get_user_defined_type` - Get struct/enum/typedef definition
- `get_type_info` - Resolve type and get details
- `get_data_decl` - Get declaration and hexdump for data

### Variables & Renaming
- `rename_function`, `rename_data` - Rename functions and data labels
- `rename_single_variable`, `rename_multi_variables` - Rename locals (batch supported)
- `retype_variable`, `set_local_variable_type` - Change variable types
- `set_function_prototype` - Set function type signature
- `get_stack_frame_vars` - Get stack frame info

### Cross References
- `get_xrefs_to` - Find code/data references to an address
- `get_xrefs_to_struct`, `get_xrefs_to_field` - Struct references
- `get_xrefs_to_type`, `get_xrefs_to_enum`, `get_xrefs_to_union`

### Comments
- `set_comment`, `get_comment`, `delete_comment` - Address comments
- `set_function_comment`, `get_function_comment`, `delete_function_comment` - Function comments

### Memory & Data
- `hexdump_address`, `hexdump_data` - View raw bytes
- `get_data_decl` - Get typed data info
- `list_data_items` - List defined data

### Modification
- `patch_bytes` - Write raw bytes (patches file and re-signs on macOS)
- `make_function_at` - Create function at address

### Number Conversion
- `convert_number` - Convert between hex/dec/bin, get C literals
- `format_value` - Add comment with conversions at address

### WoW-Specific Tools
- `wowemulation_scan_lua_api_strings` - Map Lua API to C functions
- `wowemulation_scan_rtti_entries` - Find RTTI and vtables
- `wowemulation_scan_update_fields` - Map update fields to handlers
- `wowemulation_batch_rename_functions` - Bulk rename with JSON list

### HTTP Endpoints

The plugin's HTTP server (port 9009) also exposes these endpoints directly:

- `/status`, `/binaries`, `/selectBinary` - Binary management
- `/methods`, `/imports`, `/exports`, `/classes`, `/namespaces` - Listings
- `/decompile`, `/il`, `/assembly` - Decompilation
- `/strings`, `/allStrings`, `/strings/filter` - String discovery
- `/localTypes`, `/searchTypes`, `/getUserDefinedType` - Type queries
- `/getTypeInfo`, `/getDataDecl`, `/defineTypes`, `/declareCType` - Type ops
- `/renameFunction`, `/renameData`, `/renameVariables` - Renaming
- `/retypeVariable`, `/setLocalVariableType`, `/setFunctionPrototype` - Type setting
- `/comment`, `/comment/function` - Comments
- `/getXrefsTo*`, `/getXrefsToField`, `/getXrefsToType` - References
- `/hexdump`, `/hexdumpByName`, `/data` - Data inspection
- `/patch` or `/patchBytes` - Patching
- `/convertNumber`, `/formatValue` - Number conversion
- `/makeFunctionAt`, `/platforms` - Function creation
- `/getStackFrameVars` - Stack frame info
- `/wowemulation/*` - WoW analysis tools

## Configuration

### Server Settings

The HTTP server runs on `localhost:9009` by default. These can be modified in the plugin code (`plugin/core/config.py`):

```python
@dataclass
class ServerConfig:
    host: str = "localhost"
    port: int = 9009
    debug: bool = False
```

### Binary Ninja Settings

Settings registered in Binary Ninja (`Plugins > Edit Plugin Settings > MCP`):

- `mcp.renamePrefix` - Prefix for renamed functions/variables (default: `mcp_`)
- `mcp.showStatusButton` - Show status indicator in status bar (default: `true`)

### Manual MCP Client Setup

For clients not auto-configured, use this config:

```json
{
    "mcpServers": {
        "binary_ninja_mcp": {
            "command": "/ABSOLUTE/PATH/TO/Binary Ninja/plugins/.../bridge/binja_mcp_bridge.py",
            "args": []
        }
    }
}
```

Use the script for easy management:

```bash
# Auto-setup supported clients
python scripts/mcp_client_installer.py --install

# Remove MCP entries
python scripts/mcp_client_installer.py --uninstall

# Print config snippet
python scripts/mcp_client_installer.py --config
```

## Troubleshooting

### Server won't start

1. Check Binary Ninja log (`View > Show Log Viewer`) for error messages
2. Ensure no other process is using port 9009
3. Verify Python 3.12+ is installed in Binary Ninja's environment
4. Try restarting Binary Ninja

### MCP client can't connect

1. Ensure the bridge is running (status button shows green)
2. Check that the path in MCP client config points to the correct `binja_mcp_bridge.py`
3. If using a virtual environment, the path should include the venv's Python interpreter
4. Test connectivity: `curl http://localhost:9009/status`

### Functions not found

1. Make sure a binary is loaded in Binary Ninja
2. Check that the correct binary is selected (use `list_binaries()` in your MCP client)
3. Function names are case-sensitive in Binary Ninja
4. Use `search_functions_by_name()` if unsure of the exact name

### Patching fails on macOS

The plugin automatically re-signs binaries after patching. If you see codesign errors:

1. Ensure Xcode command line tools are installed
2. Try setting `save_to_file=False` to patch in-memory only
3. Check Binary Ninja log for specific codesign error messages

### UI issues

If the status indicator doesn't appear:

1. Check that `mcp.showStatusButton` is enabled in settings
2. Restart Binary Ninja
3. The indicator may take a few seconds to appear after loading a binary

## Development

### Code Quality

This project uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting. Configuration is in `ruff.toml`.

```bash
# Check for issues
ruff check .

# Auto-fix issues
ruff check --fix .

# Check formatting issues
ruff format --check .

# Format code
ruff format .
```

### Running Ruff Manually

### Testing

This project has no automated tests. To test changes:
1. Install the plugin in Binary Ninja
2. Load a test binary
3. Start the bridge: `python bridge/binja_mcp_bridge.py`
4. Test MCP tools through your MCP client
5. Check Binary Ninja logs for errors

### Development Setup

```bash
# Clone repository
cd binary_ninja_mcp

# Install dev dependencies
pip install -r requirements.txt  # Bridge
# Plugin uses Binary Ninja's Python environment

# Run linter and formatter
ruff check .
ruff format .
```

### GitHub Actions

A GitHub Action workflow (`.github/workflows/lint-format.yml`) automatically runs Ruff on:
- Every push to `main` branch
- Every pull request targeting `main` branch

The workflow will fail if there are linting errors or formatting issues, ensuring code quality in CI.

## Prerequisites

- [Binary Ninja](https://binary.ninja/) (minimum version 4000)
- Python 3.12+
- MCP client (those with auto-setup support are listed below)

## Version Compatibility

- **Binary Ninja**: 4000+ (specified in `plugin.json`)
- **Python**: 3.12+ (specified in `pyproject.toml`)

## Security Notes

- The HTTP server only binds to `localhost` by default
- No authentication is required (assumes trusted local environment)
- Patches modify files on disk - backup important binaries before patching

## Limitations

- No automated tests - requires manual testing in Binary Ninja
- Bridge must run separately from plugin (two processes)
- Large binary files may cause timeouts in string/analysis operations
- Some Binary Ninja API features may not be exposed yet (e.g., certain IL operations)
- UI notifications may not work in headless Binary Ninja environments

## Contributing

Contributions are welcome. Please feel free to submit a pull request.

Before contributing:
1. Ensure code passes `ruff check .` and `ruff format .`
2. Test your changes manually in Binary Ninja
3. Update documentation if adding new features
4. Follow existing code conventions (see AGENTS.md)

## License

GPL-3.0-only - See [LICENSE](LICENSE) file for details.
