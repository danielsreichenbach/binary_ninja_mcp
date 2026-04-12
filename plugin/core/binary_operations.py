import platform
import re
import struct
import subprocess
import weakref
from typing import Any

import binaryninja as bn
from binaryninja.enums import StructureVariant, TypeClass

from ..utils.string_utils import escape_non_ascii
from .config import BinaryNinjaConfig


class BinaryOperations:
    def __init__(self, config: BinaryNinjaConfig):
        self.config = config
        self._current_view: bn.BinaryView | None = None
        # Multi-binary support
        # Store weak references so closed views are auto-pruned
        self._views_by_id: dict[str, weakref.ReferenceType] = {}
        self._next_view_id: int = 1
        self._id_by_filename: dict[str, str] = {}

    @property
    def current_view(self) -> bn.BinaryView | None:
        return self._current_view

    @current_view.setter
    def current_view(self, bv: bn.BinaryView | None):
        self._current_view = bv
        if bv:
            bn.log_info(f"Set current binary view: {bv.file.filename}")
            try:
                self._register_view(bv)
            except Exception:
                pass
        else:
            bn.log_info("Cleared current binary view")

    def load_binary(self, filepath: str) -> bn.BinaryView:
        """Load a binary file or bndb database.

        Uses bn.load() (BN 4.0+) with bn.open_view() as fallback.
        """
        try:
            if hasattr(bn, "load"):
                bn.log_info(f"Loading binary with bn.load: {filepath}")
                self._current_view = bn.load(filepath)
            elif hasattr(bn, "open_view"):
                bn.log_info(f"Loading binary with bn.open_view: {filepath}")
                self._current_view = bn.open_view(filepath)
            else:
                raise RuntimeError(
                    "Binary Ninja API too old: neither bn.load() nor bn.open_view() available"
                )

            if self._current_view is None:
                raise RuntimeError(f"Failed to open: {filepath}")

            try:
                self._register_view(self._current_view)
            except Exception:
                pass
            return self._current_view
        except Exception as e:
            bn.log_error(f"Failed to load binary: {e}")
            raise

    # ---------------- Multi-binary helpers ----------------
    def _prune_views(self) -> None:
        """Remove entries for BinaryViews that no longer exist and rebuild filename map."""
        alive: dict[str, weakref.ReferenceType] = {}
        new_fn_map: dict[str, str] = {}
        alive_objs: list[object] = []
        for vid, w in list(self._views_by_id.items()):
            try:
                vb = w()
            except Exception:
                vb = None
            if vb is None:
                continue
            alive[vid] = w
            alive_objs.append(vb)
            try:
                fn = str(getattr(vb.file, "filename", None)) if getattr(vb, "file", None) else None
            except Exception:
                fn = None
            if fn and fn not in new_fn_map:
                new_fn_map[fn] = vid
        self._views_by_id = alive
        self._id_by_filename = new_fn_map
        # If current_view no longer exists among alive views, clear it
        try:
            if self._current_view is not None and all(
                obj is not self._current_view for obj in alive_objs
            ):
                self._current_view = None
        except Exception:
            self._current_view = None

    def _register_view(self, bv: bn.BinaryView) -> str:
        """Add a view to the managed list if not present, return its id."""
        self._prune_views()
        # Reuse existing id if the exact object is already tracked
        for vid, w in list(self._views_by_id.items()):
            try:
                vb = w()
            except Exception:
                vb = None
            if vb is bv:
                return vid
        # Prefer deduplication by canonical filename
        fn = None
        try:
            fn = str(getattr(bv.file, "filename", None)) if getattr(bv, "file", None) else None
        except Exception:
            fn = None
        if fn:
            # If a view for this filename already exists, reuse its id and update the view
            existing_id = self._id_by_filename.get(fn)
            if existing_id and existing_id in self._views_by_id:
                # Always store weak references so closed views can be pruned
                self._views_by_id[existing_id] = weakref.ref(bv)
                return existing_id
        # Assign a new id
        vid = str(self._next_view_id)
        self._next_view_id += 1
        self._views_by_id[vid] = weakref.ref(bv)
        if fn:
            self._id_by_filename[fn] = vid
        return vid

    def register_view(self, bv: bn.BinaryView) -> str:
        """Public wrapper to register a BinaryView and return its id."""
        return self._register_view(bv)

    def unregister_by_filename(self, filename: str) -> int:
        """Remove all tracked views that match the given absolute filename.

        Returns number of entries removed.
        """
        if not filename:
            return 0
        self._prune_views()
        to_delete: list[str] = []
        for vid, w in list(self._views_by_id.items()):
            try:
                vb = w()
            except Exception:
                vb = None
            if vb is None:
                continue
            try:
                fn = getattr(vb.file, "filename", None)
            except Exception:
                fn = None
            if fn == filename:
                to_delete.append(vid)
        for vid in to_delete:
            self._views_by_id.pop(vid, None)
        # Rebuild filename map and clear current_view if it matched
        try:
            cur_fn = None
            if self._current_view and getattr(self._current_view, "file", None):
                cur_fn = getattr(self._current_view.file, "filename", None)
            if cur_fn == filename:
                self._current_view = None
        except Exception:
            self._current_view = None
        self._prune_views()
        return len(to_delete)

    def list_open_binaries(self) -> list[dict[str, str | bool]]:
        """Return a list of managed/open binaries with ids.

        Note: Tracks binaries opened via this plugin or explicitly registered as current_view.
        """
        items: list[dict[str, str | bool]] = []
        # Cleanup first
        self._prune_views()
        # Do NOT auto-register current_view here; UI monitor handles discovery.
        # This avoids re-introducing closed views via a stale strong reference.
        # Deduplicate by canonical filename; prefer the id mapped in _id_by_filename
        entries: list[tuple[str, str, bool]] = []  # (id, filename, active)
        seen: set[str] = set()
        for vid, w in self._views_by_id.items():
            try:
                vb = w()
            except Exception:
                vb = None
            if vb is None:
                continue
            try:
                fn = vb.file.filename
            except Exception:
                fn = "(unknown)"
            key = fn
            if key in seen:
                continue
            seen.add(key)
            # Resolve canonical id for this filename when available
            canonical_id = self._id_by_filename.get(fn, vid)
            try:
                vb_canon_ref = self._views_by_id.get(canonical_id)
                vb_canon = vb_canon_ref() if vb_canon_ref else vb
            except Exception:
                vb_canon = vb
            entries.append((canonical_id, fn, bool(vb_canon is self._current_view)))
        # Sort by filename for stable ordering
        entries.sort(key=lambda t: t[1] or "")
        for cid, fn, active in entries:
            items.append({"id": cid, "filename": fn, "active": active})
        return items

    def select_view(self, ident: str) -> dict[str, str] | None:
        """Select active BinaryView by id or filename/basename.

        Returns selection info on success, None on failure.
        """
        s = (ident or "").strip()
        if not s:
            return None
        self._prune_views()
        # Try id
        w = self._views_by_id.get(s)
        vb = None
        if w is not None:
            try:
                vb = w()
            except Exception:
                vb = None
        # If user passed a 1-based ordinal (from /binaries), map it to filename
        if vb is None and s.isdigit():
            try:
                idx = int(s)
                if idx >= 1:
                    lst = self.list_open_binaries()  # sorted order
                    if 1 <= idx <= len(lst):
                        fname = lst[idx - 1].get("filename")
                        if fname and isinstance(fname, str):
                            map_id = self._id_by_filename.get(fname)
                            if map_id:
                                wmap = self._views_by_id.get(map_id)
                                vb = wmap() if wmap else None
            except Exception:
                vb = None
        # Try direct filename mapping
        if vb is None:
            try:
                # Exact filename
                map_id = self._id_by_filename.get(s)
                if map_id:
                    wmap = self._views_by_id.get(map_id)
                    vb = wmap() if wmap else None
            except Exception:
                vb = None
        if vb is None:
            # Try match by full filename or basename
            for vid, w2 in self._views_by_id.items():
                try:
                    v = w2()
                except Exception:
                    v = None
                if v is None:
                    continue
                try:
                    fn = v.file.filename
                except Exception:
                    fn = None
                if not fn:
                    continue
                import os as _os

                if s == fn or s == _os.path.basename(fn):
                    vb = v
                    break
        if vb is None:
            return None
        self.current_view = vb
        vid = None
        for k, wv in self._views_by_id.items():
            try:
                vv = wv()
            except Exception:
                vv = None
            if vv is vb:
                vid = k
                break
        return {"id": vid or "", "filename": getattr(vb.file, "filename", "(unknown)")}

    def get_function_by_name_or_address(self, identifier: str | int) -> bn.Function | None:
        """Get a function by either its name or address.

        Args:
            identifier: Function name or address (can be int, hex string, or decimal string)

        Returns:
            Function object if found, None otherwise
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        # Handle address-based lookup
        try:
            if isinstance(identifier, str) and identifier.startswith("0x"):
                addr = int(identifier, 16)
            elif isinstance(identifier, (int, str)):
                addr = int(identifier) if isinstance(identifier, str) else identifier

            func = self._current_view.get_function_at(addr)
            if func:
                bn.log_info(f"Found function at address {hex(addr)}: {func.name}")
                return func
        except ValueError:
            pass

        # Handle name-based lookup with case sensitivity
        for func in self._current_view.functions:
            if func.name == identifier:
                bn.log_info(f"Found function by name: {func.name}")
                return func

        # Try case-insensitive match as fallback
        for func in self._current_view.functions:
            if func.name.lower() == str(identifier).lower():
                bn.log_info(f"Found function by case-insensitive name: {func.name}")
                return func

        # Try symbol table lookup as last resort
        symbol = self._current_view.get_symbol_by_raw_name(str(identifier))
        if symbol and symbol.address:
            func = self._current_view.get_function_at(symbol.address)
            if func:
                bn.log_info(f"Found function through symbol lookup: {func.name}")
                return func

        bn.log_error(f"Could not find function: {identifier}")
        return None

    def get_function_names(self, offset: int = 0, limit: int = 100) -> list[dict[str, str]]:
        """Get list of function names with addresses"""
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        functions = []
        for func in self._current_view.functions:
            functions.append(
                {
                    "name": func.name,
                    "address": hex(func.start),
                    "raw_name": func.raw_name if hasattr(func, "raw_name") else func.name,
                }
            )

        return functions[offset : offset + limit]

    def get_function_signature(self, identifier: str | int) -> dict[str, Any] | None:
        """Get the full signature/prototype of a function.

        Args:
            identifier: Function name or address (hex string, decimal string, or int)

        Returns:
            Dictionary with name, address, return_type, calling_convention,
            parameters, prototype, has_variable_arguments. None if not found.
        """
        func = self.get_function_by_name_or_address(identifier)
        if not func:
            return None
        ft = func.type
        cc = ft.calling_convention
        return {
            "name": func.name,
            "address": hex(func.start),
            "raw_name": func.raw_name if hasattr(func, "raw_name") else func.name,
            "return_type": str(ft.return_value),
            "calling_convention": cc.name if cc else "",
            "parameters": [
                {"name": p.name, "type": str(p.type)} for p in ft.parameters
            ],
            "prototype": str(ft),
            "has_variable_arguments": bool(ft.has_variable_arguments),
        }

    def get_class_names(self, offset: int = 0, limit: int = 100) -> list[str]:
        """Get list of class names with pagination"""
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        class_names = set()

        try:
            # Try different methods to identify classes
            for type_obj in self._current_view.types.values():
                try:
                    # Skip None or invalid types
                    if not type_obj or not hasattr(type_obj, "name"):
                        continue

                    # Method 1: Check type_class attribute
                    if hasattr(type_obj, "type_class"):
                        class_names.add(type_obj.name)
                        continue

                    # Method 2: Check structure attribute
                    if hasattr(type_obj, "structure") and type_obj.structure:
                        structure = type_obj.structure

                        # Check various attributes that indicate a class
                        if any(
                            hasattr(structure, attr)
                            for attr in [
                                "vtable",
                                "base_structures",
                                "members",
                                "functions",
                            ]
                        ):
                            class_names.add(type_obj.name)
                            continue

                        # Check type attribute if available
                        if hasattr(structure, "type"):
                            type_str = str(structure.type).lower()
                            if "class" in type_str or "struct" in type_str:
                                class_names.add(type_obj.name)
                                continue

                except Exception as e:
                    bn.log_debug(
                        f"Error processing type {getattr(type_obj, 'name', '<unknown>')}: {e}"
                    )
                    continue

            bn.log_info(f"Found {len(class_names)} classes")
            sorted_names = sorted(list(class_names))
            return sorted_names[offset : offset + limit]

        except Exception as e:
            bn.log_error(f"Error getting class names: {e}")
            return []

    def get_segments(self, offset: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Get list of segments with pagination"""
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        segments = []
        for segment in self._current_view.segments:
            segment_info = {
                "start": hex(segment.start),
                "end": hex(segment.end),
                "name": "",
                "flags": [],
            }

            # Try to get segment name if available
            if hasattr(segment, "name"):
                segment_info["name"] = segment.name
            elif hasattr(segment, "data_name"):
                segment_info["name"] = segment.data_name

            # Try to get segment flags safely
            if hasattr(segment, "flags"):
                try:
                    if isinstance(segment.flags, (list, tuple)):
                        segment_info["flags"] = list(segment.flags)
                    else:
                        segment_info["flags"] = [str(segment.flags)]
                except (AttributeError, TypeError, ValueError):
                    pass

            # Add segment permissions if available
            if hasattr(segment, "readable"):
                segment_info["readable"] = bool(segment.readable)
            if hasattr(segment, "writable"):
                segment_info["writable"] = bool(segment.writable)
            if hasattr(segment, "executable"):
                segment_info["executable"] = bool(segment.executable)

            segments.append(segment_info)

        return segments[offset : offset + limit]

    def get_sections(self, offset: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Get list of sections with pagination.

        Returns per-section fields when available:
        - name: section name
        - start/end: hex strings
        - size: integer number of bytes (end - start)
        - type: stringified section type (if exposed by BN)
        - semantics: stringified semantics (if exposed by BN)
        - linked_section: related/paired section name if exposed
        - alignment: alignment in bytes if exposed
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        results: list[dict[str, Any]] = []

        # Binary Ninja has exposed sections across versions either as an
        # iterable of Section objects or a dict-like object. Handle both.
        try:
            sec_container = getattr(self._current_view, "sections", None)
        except Exception:
            sec_container = None
        if not sec_container:
            return []

        def _iter_sections(container):
            try:
                # If it's a dict-like {name: Section}
                if hasattr(container, "items"):
                    for _name, _sec in list(container.items()):
                        yield _sec
                    return
            except Exception:
                pass
            # Otherwise assume it's iterable of Section objects
            try:
                for _sec in list(container):
                    yield _sec
            except Exception:
                return

        for sec in _iter_sections(sec_container):
            try:
                start = getattr(sec, "start", None)
                end = getattr(sec, "end", None)
                if start is None or end is None:
                    continue
                name = None
                try:
                    name = getattr(sec, "name", None)
                except Exception:
                    name = None
                try:
                    size = int(end) - int(start)
                except Exception:
                    size = None

                entry: dict[str, Any] = {
                    "name": name or "",
                    "start": hex(int(start)),
                    "end": hex(int(end)),
                    "size": size,
                }

                # Optional attributes: type, semantics, linked_section, alignment
                for attr, key in (
                    ("type", "type"),
                    ("semantics", "semantics"),
                    ("linked_section", "linked_section"),
                    ("align", "alignment"),
                    ("alignment", "alignment"),
                ):
                    try:
                        val = getattr(sec, attr, None)
                        if val is not None:
                            entry[key] = str(val)
                    except Exception:
                        pass

                results.append(entry)
            except Exception:
                continue

        return results[offset : offset + limit]

    def rename_function(self, old_name: str, new_name: str) -> bool:
        """Rename a function using multiple fallback methods.

        Args:
            old_name: Current function name or address
            new_name: New name for the function

        Returns:
            True if rename succeeded, False otherwise
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            func = self.get_function_by_name_or_address(old_name)
            if not func:
                bn.log_error(f"Function not found: {old_name}")
                return False

            bn.log_info(f"Found function to rename: {func.name} at {hex(func.start)}")

            if not new_name or not isinstance(new_name, str):
                bn.log_error(f"Invalid new name: {new_name}")
                return False

            if not hasattr(func, "name") or not hasattr(func, "__setattr__"):
                bn.log_error(f"Function {func.name} cannot be renamed (read-only)")
                return False

            try:
                # Try direct name assignment first
                old_name = func.name
                func.name = new_name

                if func.name == new_name:
                    bn.log_info(f"Successfully renamed function from {old_name} to {new_name}")
                    return True

                # Try symbol-based renaming if direct assignment fails
                if hasattr(func, "symbol") and func.symbol:
                    try:
                        new_symbol = bn.Symbol(
                            func.symbol.type,
                            func.start,
                            new_name,
                            namespace=func.symbol.namespace
                            if hasattr(func.symbol, "namespace")
                            else None,
                        )
                        self._current_view.define_user_symbol(new_symbol)
                        bn.log_info("Successfully renamed function using symbol table")
                        return True
                    except Exception as e:
                        bn.log_error(f"Symbol-based rename failed: {e}")

                # Try function update method as last resort
                if hasattr(self._current_view, "update_function"):
                    try:
                        func_copy = func
                        func_copy.name = new_name
                        self._current_view.update_function(func)
                        bn.log_info("Successfully renamed function using update method")
                        return True
                    except Exception as e:
                        bn.log_error(f"Function update rename failed: {e}")

                bn.log_error(f"All rename methods failed - function name unchanged: {func.name}")
                return False

            except Exception as e:
                bn.log_error(f"Error during rename operation: {e}")
                return False

        except Exception as e:
            bn.log_error(f"Error in rename_function: {e}")
            return False

    def get_function_info(self, identifier: str | int) -> dict[str, Any] | None:
        """Get detailed information about a function"""
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        func = self.get_function_by_name_or_address(identifier)
        if not func:
            return None

        bn.log_info(f"Found function: {func.name} at {hex(func.start)}")

        info = {
            "name": func.name,
            "raw_name": func.raw_name if hasattr(func, "raw_name") else func.name,
            "address": hex(func.start),
            "symbol": None,
        }

        if func.symbol:
            info["symbol"] = {
                "type": str(func.symbol.type),
                "full_name": func.symbol.full_name
                if hasattr(func.symbol, "full_name")
                else func.symbol.name,
            }

        return info

    def decompile_function(self, identifier: str | int) -> str | None:
        """Decompile a function and include addresses per statement.

        Args:
            identifier: Function name or address

        Returns:
            Decompiled HLIL-like code with address prefixes per line
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        func = self.get_function_by_name_or_address(identifier)
        if not func:
            return None

        # analyze func in case it was skipped
        func.analysis_skipped = False
        self._current_view.update_analysis_and_wait()

        try:
            il = getattr(func, "hlil", None)
            if il and hasattr(il, "instructions"):
                lines: list[str] = []
                last_addr: int | None = None
                for ins in il.instructions:
                    try:
                        addr = getattr(ins, "address", None)
                    except Exception:
                        addr = None
                    if addr is None:
                        addr = last_addr if last_addr is not None else func.start
                    last_addr = addr
                    addr_str = f"{int(addr):08x}"
                    text = str(ins)
                    lines.append(f"{addr_str}        {text}")
                return "\n".join(lines)
            # Fall back to MLIL with addresses
            mil = getattr(func, "mlil", None)
            if mil and hasattr(mil, "instructions"):
                lines: list[str] = []
                last_addr: int | None = None
                for ins in mil.instructions:
                    try:
                        addr = getattr(ins, "address", None)
                    except Exception:
                        addr = None
                    if addr is None:
                        addr = last_addr if last_addr is not None else func.start
                    last_addr = addr
                    addr_str = f"{int(addr):08x}"
                    text = str(ins)
                    lines.append(f"{addr_str}        {text}")
                return "\n".join(lines)
            # Last resort
            return str(func)
        except Exception as e:
            bn.log_error(f"Error decompiling function: {e!s}")
            return None

    def get_function_il(
        self, identifier: str | int, view: str = "hlil", ssa: bool = False
    ) -> str | None:
        """Return IL for a function with selectable view and optional SSA form.

        Args:
            identifier: Function name or address
            view: One of 'hlil', 'mlil', 'llil' (case-insensitive). Aliases: 'il' -> 'llil'.
            ssa: When True, use SSA form if available (MLIL/LLIL only)

        Returns:
            Concatenated string with one instruction per line prefixed by address.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        func = self.get_function_by_name_or_address(identifier)
        if not func:
            return None

        # Ensure analysis has run for this function
        try:
            func.analysis_skipped = False
            self._current_view.update_analysis_and_wait()
        except Exception:
            pass

        v = (view or "").strip().lower()
        if v in ("il", "llil", "low", "lowlevel", "low-level", "low_level"):
            prop = "llil"
        elif v in ("mlil", "medium", "mediumlevel", "medium-level", "medium_level"):
            prop = "mlil"
        else:
            # Default to HLIL when unknown
            prop = "hlil"

        try:
            il_func = getattr(func, prop, None)
            if il_func is None:
                return None

            # Only MLIL/LLIL support SSA form in practice
            if ssa and hasattr(il_func, "ssa_form") and il_func.ssa_form is not None:
                il_func = il_func.ssa_form

            if not hasattr(il_func, "instructions"):
                # As a last resort, stringify the object
                return str(il_func)

            lines: list[str] = []
            last_addr: int | None = None
            for ins in il_func.instructions:
                try:
                    addr = getattr(ins, "address", None)
                except Exception:
                    addr = None
                if addr is None:
                    addr = last_addr if last_addr is not None else func.start
                last_addr = addr
                addr_str = f"{int(addr):08x}"
                text = str(ins)
                lines.append(f"{addr_str}        {text}")
            return "\n".join(lines)
        except Exception as e:
            bn.log_error(
                f"Error getting {prop}{' SSA' if ssa else ''} for function {identifier}: {e!s}"
            )
            return None

    def rename_data(self, address: int, new_name: str) -> bool:
        """Rename data at a specific address"""
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            if self._current_view.is_valid_offset(address):
                self._current_view.define_user_symbol(
                    bn.Symbol(bn.SymbolType.DataSymbol, address, new_name)
                )
                return True
        except Exception as e:
            bn.log_error(f"Failed to rename data: {e}")
        return False

    def make_function_at(
        self, address: str | int, architecture: str | None = None
    ) -> dict[str, Any]:
        """Create a function at the given address (no-op if it already exists).

        Args:
            address: Hex string (e.g., 0x401000) or integer address.
            architecture: Optional architecture name (e.g., "x86_64", "x86", "armv7").

        Returns:
            Dict with keys: status (ok|exists), address, name (if found), architecture (if resolved).

        Raises:
            RuntimeError if no binary is loaded.
            ValueError on invalid address or creation failure.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        # Parse address
        try:
            if isinstance(address, str) and address.lower().startswith("0x"):
                addr = int(address, 16)
            else:
                addr = int(address)
        except Exception:
            raise ValueError(f"Invalid address: {address}")

        bv = self._current_view

        # If a function already exists, return info
        try:
            existing = bv.get_function_at(addr)
            if existing:
                return {
                    "status": "exists",
                    "address": hex(addr),
                    "name": existing.name,
                    "architecture": str(getattr(existing, "arch", getattr(bv, "arch", ""))) or None,
                }
        except Exception:
            pass

        # Resolve platform if provided; otherwise use view/platform default.
        # Note: BinaryView.create_user_function expects a Platform, not an Architecture.
        plat_obj = None
        arch_token = None
        if isinstance(architecture, str):
            arch_token = architecture.strip().lower()
        if architecture and arch_token not in (None, "", "default", "auto", "platform"):
            try:
                P = getattr(__import__("binaryninja", fromlist=["Platform"]), "Platform", None)
            except Exception:
                P = None
            if P is not None:
                try:
                    plat_obj = P[architecture]
                except Exception:
                    try:
                        getp = getattr(P, "get_by_name", None)
                        if callable(getp):
                            plat_obj = getp(architecture)
                    except Exception:
                        plat_obj = None
            # If user explicitly provided an architecture/platform name and we couldn't resolve it,
            # return an error with suggestions instead of silently using the default.
            if plat_obj is None:
                import re as _re
                from difflib import get_close_matches as _gcm

                names: list[str] = []
                # Prefer dynamic enumeration via binaryninja.Platform
                try:
                    import binaryninja as _bn  # type: ignore

                    try:
                        names = [
                            str(getattr(p, "name", str(p))) for p in list(getattr(_bn, "Platform"))
                        ]
                    except Exception:
                        names = []
                except Exception:
                    names = []
                # Fallback: try iterating via imported P if available
                if not names and P is not None:
                    try:
                        names = [str(getattr(p, "name", str(p))) for p in list(P)]
                    except Exception:
                        names = []
                # Last resort: static catalog (kept up-to-date best-effort)
                if not names:
                    names = [
                        "decree-x86",
                        "efi-x86",
                        "efi-windows-x86",
                        "efi-x86_64",
                        "efi-windows-x86_64",
                        "efi-aarch64",
                        "efi-windows-aarch64",
                        "efi-armv7",
                        "efi-thumb2",
                        "freebsd-x86",
                        "freebsd-x86_64",
                        "freebsd-aarch64",
                        "freebsd-armv7",
                        "freebsd-thumb2",
                        "ios-aarch64",
                        "ios-armv7",
                        "ios-thumb2",
                        "ios-kernel-aarch64",
                        "ios-kernel-armv7",
                        "ios-kernel-thumb2",
                        "linux-ppc32",
                        "linux-ppcvle32",
                        "linux-ppc64",
                        "linux-ppc32_le",
                        "linux-ppc64_le",
                        "linux-rv32gc",
                        "linux-rv64gc",
                        "linux-x86",
                        "linux-x86_64",
                        "linux-x32",
                        "linux-aarch64",
                        "linux-armv7",
                        "linux-thumb2",
                        "linux-armv7eb",
                        "linux-thumb2eb",
                        "linux-mipsel",
                        "linux-mips",
                        "linux-mips3",
                        "linux-mipsel3",
                        "linux-mips64",
                        "linux-cnmips64",
                        "linux-mipsel64",
                        "mac-x86",
                        "mac-x86_64",
                        "mac-aarch64",
                        "mac-armv7",
                        "mac-thumb2",
                        "mac-kernel-x86",
                        "mac-kernel-x86_64",
                        "mac-kernel-aarch64",
                        "mac-kernel-armv7",
                        "mac-kernel-thumb2",
                        "windows-x86",
                        "windows-x86_64",
                        "windows-aarch64",
                        "windows-armv7",
                        "windows-thumb2",
                        "windows-kernel-x86",
                        "windows-kernel-x86_64",
                        "windows-kernel-windows-aarch64",
                    ]
                # Build ranked suggestions
                tl = (arch_token or "").lower()

                def _score(n: str) -> float:
                    nl = n.lower()
                    s = 0.0
                    if tl and tl in nl:
                        s += 2.0
                    # remove non-alnum for loose matching
                    tlr = _re.sub(r"[^a-z0-9]", "", tl)
                    nlr = _re.sub(r"[^a-z0-9]", "", nl)
                    if tlr and tlr in nlr:
                        s += 1.0
                    return s

                base = sorted(names)
                # Start with substring matches, then extend with close matches
                substr = [n for n in base if tl in n.lower()]
                # Use difflib for additional candidates if needed
                extra = _gcm(tl, base, n=10, cutoff=0.3) if tl else []
                cand = []
                seen = set()
                for n in substr + extra:
                    if n not in seen:
                        seen.add(n)
                        cand.append(n)
                cand.sort(key=_score, reverse=True)
                cand[:10]
                raise ValueError(f"Unknown platform/architecture '{architecture}'")
        # Default/platform fallback when no explicit architecture provided
        if plat_obj is None:
            try:
                plat_obj = getattr(bv, "platform", None)
            except Exception:
                plat_obj = None

        # Create the function
        try:
            if hasattr(bv, "create_user_function"):
                if plat_obj is not None:
                    bv.create_user_function(addr, plat_obj)
                else:
                    bv.create_user_function(addr)
            elif hasattr(bv, "add_function"):
                if plat_obj is not None:
                    bv.add_function(addr, plat_obj)
                else:
                    bv.add_function(addr)
            else:
                raise ValueError("BinaryView does not support function creation")
        except Exception as e:
            raise ValueError(f"Failed to create function: {e!s}")

        # Fetch created function info
        try:
            fn = bv.get_function_at(addr)
        except Exception:
            fn = None
        return {
            "status": "ok",
            "address": hex(addr),
            "name": fn.name if fn else None,
            "platform": str(plat_obj) if plat_obj is not None else None,
            "architecture": str(getattr(plat_obj, "arch", None))
            if plat_obj is not None
            else (
                str(getattr(bv, "arch", None)) if getattr(bv, "arch", None) is not None else None
            ),
        }

    def get_defined_data(
        self, offset: int = 0, limit: int = 100, read_len: int = 32,
        filter_name: str = ""
    ) -> list[dict[str, Any]]:
        """Get list of defined data variables with lightweight previews and sizes.

        Returns per-item fields:
        - address: hex string
        - name/raw_name: label info if available
        - type: string if available
        - size: exact defined size in bytes if known (from BN type)
        - width: alias of size for backward compatibility
        - value: small integer value when width<=8 and readable; otherwise None
        - bytes_hex: hex string of up to preview_len bytes
        - ascii_preview: printable ASCII representation for the same bytes
        - repr: concise, human-friendly summary for LLMs (value/ASCII/hex)

        When filter_name is non-empty, only data items whose symbol name
        contains the substring (case-insensitive) are returned. Pagination
        is applied during iteration to avoid processing all items.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        data_items = []
        filter_lower = filter_name.lower() if filter_name else ""
        matched = 0  # count of items passing the filter

        for var in self._current_view.data_vars:
            # Early filter: check symbol name before expensive processing
            if filter_lower:
                sym = self._current_view.get_symbol_at(var)
                sym_name = sym.name if sym else ""
                if filter_lower not in sym_name.lower():
                    continue
            else:
                sym = None  # defer symbol lookup to later

            # Pagination: skip items before offset
            if matched < offset:
                matched += 1
                continue
            # Stop once we have enough items
            if len(data_items) >= limit:
                break
            matched += 1
            data_type = None  # may be a BN Type or a DataVariable
            value = None
            width = None
            bytes_hex = None
            ascii_preview = None
            typ_obj = None

            try:
                # Prefer DataVariable (carries underlying Type)
                dv = None
                if hasattr(self._current_view, "get_data_var_at"):
                    try:
                        dv = self._current_view.get_data_var_at(var)
                    except Exception:
                        dv = None
                if dv is not None and hasattr(dv, "type") and dv.type is not None:
                    typ_obj = dv.type
                    data_type = dv  # keep for fallback string formatting
                else:
                    # Fall back to direct type lookup
                    if hasattr(self._current_view, "get_type_at"):
                        try:
                            typ_obj = self._current_view.get_type_at(var)
                            data_type = typ_obj
                        except Exception:
                            typ_obj = None

                # Exact defined size if available
                if typ_obj is not None and hasattr(typ_obj, "width"):
                    try:
                        width = int(typ_obj.width)
                    except Exception:
                        width = None

                # Best-effort numeric read for small integers (<= 8 bytes)
                if width is not None and width <= 8:
                    try:
                        value = str(self._current_view.read_int(var, width))
                    except (ValueError, RuntimeError):
                        value = None

                # Provide bytes + ASCII preview for all cases
                # Determine effective read length
                try:
                    requested = int(read_len)
                except Exception:
                    requested = 32
                # If requested < 0 and width known, treat as "read exact size"
                if requested < 0 and width is not None:
                    eff_len = max(0, int(width))
                else:
                    eff_len = max(0, requested if requested >= 0 else 32)
                if width is not None:
                    eff_len = min(eff_len, int(width))

                try:
                    raw = self._current_view.read(var, eff_len)
                    if raw is not None:
                        try:
                            bytes_hex = raw.hex()
                        except Exception:
                            bytes_hex = None
                        try:
                            ascii_preview = "".join(chr(b) if 32 <= b <= 126 else "." for b in raw)
                        except Exception:
                            ascii_preview = None
                except (ValueError, RuntimeError, TypeError):
                    pass
            except (AttributeError, TypeError, ValueError, RuntimeError):
                value = None
                data_type = None
                typ_obj = None

            # If BN doesn't expose a width, try to infer size from call sites.
            # Skip this expensive HLIL analysis when filtering (caller wants
            # discovery, not precise sizing).
            if width is None and not filter_lower:
                try:
                    inferred = self.infer_data_size(int(var))
                    if isinstance(inferred, int) and inferred > 0:
                        width = inferred
                except Exception:
                    pass

            # Get symbol information (may already be resolved by filter)
            if sym is None:
                sym = self._current_view.get_symbol_at(var)
            # Choose a concise repr for LLMs
            if value is not None:
                short_repr = f"int:{value}"
            elif ascii_preview:
                short_repr = f'ascii:"{ascii_preview}"'
            elif bytes_hex:
                short_repr = f"hex:{bytes_hex}"
            else:
                short_repr = None

            data_items.append(
                {
                    "address": hex(var),
                    "name": sym.name if sym else "(unnamed)",
                    "raw_name": sym.raw_name if sym and hasattr(sym, "raw_name") else None,
                    # Prefer clean type string (avoid "<var ...>" envelope when possible)
                    "type": (
                        str(typ_obj)
                        if typ_obj is not None
                        else (str(data_type) if data_type else None)
                    ),
                    "size": width,
                    "width": width,
                    "value": value,
                    "bytes_hex": bytes_hex,
                    "ascii_preview": ascii_preview,
                    "bytes_read": len(bytes_hex) // 2 if bytes_hex else 0,
                    "repr": short_repr,
                }
            )

        return data_items

    def infer_data_size(self, address: int) -> int | None:
        """Infer size for data at address when BN hasn't defined a type width.

        Strategy:
        - Prefer BN's DataVariable.type.width or get_type_at().width if available.
        - Otherwise scan HLIL for calls like memcmp/strncmp/memcpy/strncpy where
          an argument equals this address and extract the last numeric argument
          as a best-effort length. Returns the maximum constant seen.
        """
        if not self._current_view:
            return None

        # 1) BN-provided width if available
        try:
            dv = None
            if hasattr(self._current_view, "get_data_var_at"):
                dv = self._current_view.get_data_var_at(address)
            t = None
            if dv is not None and hasattr(dv, "type"):
                t = dv.type
            elif hasattr(self._current_view, "get_type_at"):
                t = self._current_view.get_type_at(address)
            if t is not None and hasattr(t, "width") and t.width:
                return int(t.width)
        except Exception:
            pass

        # 2) HLIL heuristic
        try:
            addr_hex = hex(address)
            candidates: list[int] = []
            names = ("memcmp", "strncmp", "memcpy", "strncpy")
            for func in list(self._current_view.functions):
                try:
                    il = getattr(func, "hlil", None)
                    if not il:
                        continue
                    for ins in il.instructions:
                        try:
                            text = str(ins)
                            if addr_hex not in text:
                                continue
                            if not any(n in text for n in names):
                                continue
                            # Extract all numeric constants
                            nums = re.findall(r"0x[0-9a-fA-F]+|\b\d+\b", text)
                            vals: list[int] = []
                            for n in nums:
                                try:
                                    v = int(n, 16) if n.startswith("0x") else int(n)
                                    vals.append(v)
                                except Exception:
                                    continue
                            if vals:
                                # Heuristic: last constant in call string is likely the size
                                candidates.append(vals[-1])
                        except Exception:
                            continue
                except Exception:
                    continue
            if candidates:
                # Use the maximum plausible size
                best = max(c for c in candidates if c > 0)
                if best > 0:
                    return best
        except Exception:
            pass
        return None

    def list_local_types(
        self, offset: int = 0, limit: int = 100, include_libraries: bool = False
    ) -> list[dict[str, Any]]:
        """List local types (Types view) in the current database.

        Returns a list of dictionaries with:
        - name: type name
        - kind: struct/union/class/enum/typedef/unknown
        - decl: string form of the type (when available)
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        results: list[dict[str, Any]] = []
        seen_keys = set()
        try:

            def add_type_entry(name, tobj):
                # Normalize name to string to avoid BN QualifiedName in JSON
                try:
                    name_str = str(name) if name is not None else None
                except Exception:
                    name_str = None
                if not name_str:
                    return
                # Fallback: try to resolve missing type object by querying BV / libraries
                if tobj is None:
                    try:
                        if self._current_view is not None and hasattr(
                            self._current_view, "get_type_by_name"
                        ):
                            t2 = self._current_view.get_type_by_name(name_str)
                            if t2 is not None:
                                tobj = t2
                    except Exception:
                        pass
                    if tobj is None:
                        try:
                            plat = getattr(self._current_view, "platform", None)
                            libs = list(getattr(plat, "type_libraries", []) or []) if plat else []
                            for lib in libs:
                                get_t = getattr(lib, "get_type_by_name", None)
                                if not callable(get_t):
                                    continue
                                t3 = None
                                try:
                                    # Try QualifiedName if available
                                    try:
                                        t3 = get_t(bn.QualifiedName(name_str))
                                    except Exception:
                                        t3 = get_t(name_str)
                                except Exception:
                                    t3 = None
                                if t3 is not None:
                                    tobj = t3
                                    break
                        except Exception:
                            pass

                tc = getattr(tobj, "type_class", None)
                kind = "unknown"
                if tc == TypeClass.VoidTypeClass:
                    kind = "void"
                elif tc == TypeClass.BoolTypeClass:
                    kind = "bool"
                elif tc == TypeClass.IntegerTypeClass:
                    kind = "int"
                elif tc == TypeClass.FloatTypeClass:
                    kind = "float"
                elif tc == TypeClass.StructureTypeClass:
                    try:
                        if getattr(tobj, "type", None) == StructureVariant.StructStructureType:
                            kind = "struct"
                        elif getattr(tobj, "type", None) == StructureVariant.UnionStructureType:
                            kind = "union"
                        elif getattr(tobj, "type", None) == StructureVariant.ClassStructureType:
                            kind = "class"
                        else:
                            kind = "struct"
                    except Exception:
                        kind = "struct"
                elif tc == TypeClass.EnumerationTypeClass:
                    kind = "enum"
                elif tc == TypeClass.NamedTypeReferenceClass:
                    kind = "typedef"
                elif tc == TypeClass.FunctionTypeClass:
                    kind = "function"
                elif tc == TypeClass.WideCharTypeClass:
                    kind = "wchar"
                elif tc == TypeClass.PointerTypeClass:
                    kind = "pointer"
                elif tc == TypeClass.ArrayTypeClass:
                    kind = "array"

                decl = None
                try:
                    decl = str(tobj)
                except Exception:
                    try:
                        decl = str(getattr(tobj, "type", None))
                    except Exception:
                        decl = None

                # If kind is unknown or a named typedef, try to infer underlying from declaration text
                try:
                    dlow = (decl or "").strip().lower()
                    if dlow:
                        if dlow.startswith("struct ") or " struct " in dlow:
                            kind = "struct"
                        elif dlow.startswith("union ") or " union " in dlow:
                            kind = "union"
                        elif dlow.startswith("enum ") or " enum " in dlow:
                            kind = "enum"
                except Exception:
                    pass

                key = (name_str, decl or "")
                if key in seen_keys:
                    return
                results.append(
                    {
                        "name": name_str,
                        "kind": kind,
                        "type_class": str(tc) if tc is not None else None,
                        "decl": decl,
                    }
                )
                seen_keys.add(key)

            # Source 1: user_type_container (explicit local/user types)
            try:
                utc = getattr(self._current_view, "user_type_container", None)
                if utc and getattr(utc, "types", None):
                    for type_id in list(utc.types.keys()):
                        try:
                            entry = utc.types[type_id]
                            name = (
                                entry[0]
                                if isinstance(entry, (tuple, list))
                                else getattr(entry, "name", None)
                            )
                            tobj = (
                                entry[1]
                                if isinstance(entry, (tuple, list))
                                else getattr(entry, "type", entry)
                            )
                            add_type_entry(name, tobj)
                        except Exception:
                            continue
            except Exception:
                pass

            # Source 2: view.types (BN view-local types)
            for k, v in self._current_view.types.items():
                try:
                    if isinstance(v, (tuple, list)) and len(v) >= 2:
                        name = str(v[0])
                        tobj = v[1]
                    else:
                        tobj = v
                        name = getattr(v, "name", None)
                        if not name:
                            name = str(k)
                    add_type_entry(name, tobj)
                except Exception:
                    continue

            # Source 3: platform type libraries (optional; can be heavy)
            if include_libraries:
                try:
                    plat = getattr(self._current_view, "platform", None)
                    libs = []
                    try:
                        libs = list(getattr(plat, "type_libraries", []) or [])
                    except Exception:
                        libs = []
                    for lib in libs:
                        # Try multiple ways to enumerate names in this library
                        names = []
                        try:
                            nt = getattr(lib, "named_types", None)
                            if isinstance(nt, dict):
                                names = list(nt.keys())
                        except Exception:
                            pass
                        if not names:
                            try:
                                tmap = getattr(lib, "types", None)
                                if isinstance(tmap, dict):
                                    names = list(tmap.keys())
                            except Exception:
                                pass
                        if not names:
                            try:
                                get_names = getattr(lib, "get_type_names", None)
                                if callable(get_names):
                                    result = get_names()
                                    if hasattr(result, "__iter__"):
                                        names = list(result)  # type: ignore[arg-type]
                            except Exception:
                                pass
                        # Fetch each type object if possible
                        for nm in names:
                            try:
                                tobj = None
                                try:
                                    g = getattr(lib, "get_type_by_name", None)
                                    if callable(g):
                                        tobj = g(nm)
                                except Exception:
                                    tobj = None
                                add_type_entry(nm, tobj)
                            except Exception:
                                continue
                except Exception:
                    pass
        except Exception as e:
            bn.log_error(f"Error listing local types: {e}")
        return results[offset : offset + limit]

    def search_local_types(
        self, query: str, offset: int = 0, limit: int = 100, include_libraries: bool = False
    ) -> list[dict[str, Any]]:
        """Search local/view types whose name or declaration contains the substring.

        Returns entries with {name, kind, type_class, decl}.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        if not query:
            return []
        ql = str(query).lower()
        # Only local types by default (fast). Optionally include libraries.
        all_types = self.list_local_types(0, 1_000_000, include_libraries=include_libraries)
        matches: list[dict[str, Any]] = []
        for t in all_types:
            try:
                name = t.get("name") or ""
                decl = t.get("decl") or ""
                if (ql in str(name).lower()) or (ql in str(decl).lower()):
                    matches.append(t)
            except Exception:
                continue
        if isinstance(limit, int) and limit < 0:
            return matches[offset:]
        return matches[offset : offset + limit]

    def get_type_info(self, name: str) -> dict[str, Any]:
        """Resolve a type by name and return detailed information.

        Returns a dictionary with:
        - name: type name
        - kind: struct/union/class/enum/typedef/... (best-effort)
        - decl: declaration string
        - members: for struct/union [{name, type, offset}]
        - enum_members: for enums [{name, value}]
        - underlying: for typedefs, best-effort underlying declaration
        - source: local | library | unknown
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        type_name = str(name)
        tobj = None
        source = "unknown"

        # 1) Try view local resolution first
        try:
            if hasattr(self._current_view, "get_type_by_name"):
                t = self._current_view.get_type_by_name(type_name)
                if t is not None:
                    tobj = t
                    source = "local"
        except Exception:
            pass

        # 2) Fall back to platform type libraries
        if tobj is None:
            try:
                plat = getattr(self._current_view, "platform", None)
                libs = list(getattr(plat, "type_libraries", []) or []) if plat else []
                for lib in libs:
                    get_t = getattr(lib, "get_type_by_name", None)
                    if not callable(get_t):
                        continue
                    try:
                        # Try with QualifiedName if available
                        try:
                            t = get_t(bn.QualifiedName(type_name))
                        except Exception:
                            t = get_t(type_name)
                    except Exception:
                        t = None
                    if t is not None:
                        tobj = t
                        source = "library"
                        break
            except Exception:
                pass

        # Prepare defaults
        kind = "unknown"
        decl = None
        members: list[dict[str, Any]] = []
        enum_members: list[dict[str, Any]] = []
        underlying = None

        # Extract details from type object
        if tobj is not None:
            try:
                decl = str(tobj)
            except Exception:
                try:
                    decl = str(getattr(tobj, "type", None))
                except Exception:
                    decl = None

            tc = getattr(tobj, "type_class", None)
            if tc == TypeClass.StructureTypeClass:
                # structure variant
                try:
                    v = getattr(tobj, "type", None)
                    if v == StructureVariant.UnionStructureType:
                        kind = "union"
                    elif v == StructureVariant.ClassStructureType:
                        kind = "class"
                    else:
                        kind = "struct"
                except Exception:
                    kind = "struct"

                # collect members
                try:
                    for m in getattr(
                        tobj, "members", getattr(getattr(tobj, "structure", None), "members", [])
                    ):
                        try:
                            members.append(
                                {
                                    "name": getattr(m, "name", None),
                                    "type": str(getattr(m, "type", ""))
                                    if hasattr(m, "type")
                                    else None,
                                    "offset": int(getattr(m, "offset", 0))
                                    if hasattr(m, "offset")
                                    else None,
                                }
                            )
                        except Exception:
                            continue
                except Exception:
                    pass

            elif tc == TypeClass.EnumerationTypeClass:
                kind = "enum"
                try:
                    for em in getattr(tobj, "members", []):
                        try:
                            enum_members.append(
                                {
                                    "name": getattr(em, "name", None),
                                    "value": getattr(em, "value", None),
                                }
                            )
                        except Exception:
                            continue
                except Exception:
                    pass

            elif tc == TypeClass.NamedTypeReferenceClass:
                kind = "typedef"
                # best-effort underlying from decl text
                try:
                    dlow = (decl or "").lower()
                    if dlow:
                        if dlow.startswith("struct ") or " struct " in dlow:
                            underlying = "struct"
                        elif dlow.startswith("union ") or " union " in dlow:
                            underlying = "union"
                        elif dlow.startswith("enum ") or " enum " in dlow:
                            underlying = "enum"
                except Exception:
                    pass

            elif tc == TypeClass.IntegerTypeClass:
                kind = "int"
            elif tc == TypeClass.FloatTypeClass:
                kind = "float"
            elif tc == TypeClass.BoolTypeClass:
                kind = "bool"
            elif tc == TypeClass.VoidTypeClass:
                kind = "void"
            elif tc == TypeClass.PointerTypeClass:
                kind = "pointer"
            elif tc == TypeClass.ArrayTypeClass:
                kind = "array"
            elif tc == TypeClass.FunctionTypeClass:
                kind = "function"

            # Infer kind from decl if still unknown
            if kind == "unknown" and decl:
                try:
                    dl = decl.lower()
                    if dl.startswith("struct ") or " struct " in dl:
                        kind = "struct"
                    elif dl.startswith("union ") or " union " in dl:
                        kind = "union"
                    elif dl.startswith("enum ") or " enum " in dl:
                        kind = "enum"
                except Exception:
                    pass

        return {
            "name": type_name,
            "kind": kind,
            "decl": decl,
            "members": members if members else None,
            "enum_members": enum_members if enum_members else None,
            "underlying": underlying,
            "source": source,
        }

    def export_analysis(
        self,
        include_prototypes: bool = True,
        include_types: bool = True,
        include_data_types: bool = True,
        include_comments: bool = True,
    ) -> dict[str, Any]:
        """Export all user analysis for the active binary.

        Returns a dictionary with sections controlled by the boolean flags:
        - functions: name, address, raw_name, prototype, return_type,
          calling_convention, parameters (name + type per param)
        - types: full type info per user-defined type (reuses get_type_info)
        - data_labels: address, name, type for each data variable
        - comments: address comments and function comments
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        bv = self._current_view
        result: dict[str, Any] = {
            "binary": bv.file.filename,
        }

        if include_prototypes:
            functions = []
            for func in bv.functions:
                entry: dict[str, Any] = {
                    "name": func.name,
                    "address": hex(func.start),
                    "raw_name": func.raw_name if hasattr(func, "raw_name") else func.name,
                }
                try:
                    ft = func.type
                    cc = ft.calling_convention
                    entry["prototype"] = str(ft)
                    entry["return_type"] = str(ft.return_value)
                    entry["calling_convention"] = cc.name if cc else ""
                    entry["parameters"] = [
                        {"name": p.name, "type": str(p.type)} for p in ft.parameters
                    ]
                    entry["has_variable_arguments"] = bool(ft.has_variable_arguments)
                except Exception:
                    entry["prototype"] = None
                functions.append(entry)
            result["functions"] = functions

        if include_types:
            types = []
            try:
                for type_name in bv.types:
                    try:
                        info = self.get_type_info(str(type_name))
                        types.append(info)
                    except Exception:
                        continue
            except Exception:
                pass
            result["types"] = types

        if include_data_types:
            data_labels = []
            try:
                for addr, var in bv.data_vars.items():
                    entry = {
                        "address": hex(addr),
                        "type": str(var.type) if var.type else None,
                    }
                    sym = bv.get_symbol_at(addr)
                    entry["name"] = sym.name if sym else None
                    data_labels.append(entry)
            except Exception:
                pass
            result["data_labels"] = data_labels

        if include_comments:
            comments: dict[str, Any] = {}

            # Address comments
            addr_comments = {}
            try:
                for addr, text in bv.address_comments.items():
                    addr_comments[hex(addr)] = text
            except Exception:
                pass
            comments["address"] = addr_comments

            # Function comments
            func_comments = {}
            try:
                for func in bv.functions:
                    if func.comment:
                        func_comments[hex(func.start)] = func.comment
            except Exception:
                pass
            comments["function"] = func_comments

            result["comments"] = comments

        return result

    def get_strings(self, offset: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Get list of strings in the current binary view with pagination.

        Returns a list of dictionaries containing:
        - address: start address of the string (hex)
        - length: length in bytes (int if available)
        - type: Binary Ninja string type (str if available)
        - value: best-effort decoded and escaped string value
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        results: list[dict[str, Any]] = []

        try:
            # Prefer modern API if available
            strings_iter = None
            if hasattr(self._current_view, "get_strings"):
                try:
                    strings_iter = self._current_view.get_strings()
                except TypeError:
                    strings_iter = None

            if strings_iter is None and hasattr(self._current_view, "strings"):
                try:
                    strings_iter = list(self._current_view.strings)
                except Exception:
                    strings_iter = []

            if strings_iter is None:
                strings_iter = []

            for s in strings_iter:
                try:
                    addr = None
                    length = None
                    stype = None
                    value = None

                    # Common attributes on StringReference
                    addr = getattr(s, "start", getattr(s, "address", None))
                    length = getattr(s, "length", None)
                    stype = getattr(s, "type", None)
                    if stype is not None:
                        try:
                            stype = str(stype)
                        except Exception:
                            stype = str(stype)

                    value = getattr(s, "value", None)

                    # Best-effort read/decode if value is not present
                    if value is None and addr is not None and length is not None:
                        try:
                            raw = self._current_view.read(addr, length)
                            # Stop at first null byte if present
                            nul = raw.find(b"\x00")
                            if nul != -1:
                                raw = raw[:nul]
                            try:
                                value = raw.decode("utf-8", errors="ignore")
                            except Exception:
                                value = raw.decode("latin-1", errors="ignore")
                        except Exception:
                            value = None

                    # Ensure value is a string and escape non-ASCII
                    if value is None:
                        value = ""
                    value = escape_non_ascii(str(value))

                    results.append(
                        {
                            "address": hex(addr)
                            if isinstance(addr, int)
                            else (str(addr) if addr is not None else None),
                            "length": int(length)
                            if isinstance(length, (int,))
                            else (None if length is None else int(length)),
                            "type": stype,
                            "value": value,
                        }
                    )
                except Exception as e:
                    # Keep collecting even if one entry fails
                    bn.log_debug(f"Error processing string entry: {e}")
                    continue

            return results[offset : offset + limit]
        except Exception as e:
            bn.log_error(f"Error getting strings: {e}")
            return []

    def set_comment(self, address: int, comment: str) -> bool:
        """Set a comment at a specific address.

        Args:
            address: The address to set the comment at
            comment: The comment text to set

        Returns:
            True if the comment was set successfully, False otherwise
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            if not self._current_view.is_valid_offset(address):
                bn.log_error(f"Invalid address for comment: {hex(address)}")
                return False

            self._current_view.set_comment_at(address, comment)
            bn.log_info(f"Set comment at {hex(address)}: {comment}")
            return True
        except Exception as e:
            bn.log_error(f"Failed to set comment: {e}")
            return False

    def set_function_comment(self, identifier: str | int, comment: str) -> bool:
        """Set a comment for a function.

        Args:
            identifier: Function name or address
            comment: The comment text to set

        Returns:
            True if the comment was set successfully, False otherwise
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            func = self.get_function_by_name_or_address(identifier)
            if not func:
                bn.log_error(f"Function not found: {identifier}")
                return False

            self._current_view.set_comment_at(func.start, comment)
            bn.log_info(f"Set comment for function {func.name} at {hex(func.start)}: {comment}")
            return True
        except Exception as e:
            bn.log_error(f"Failed to set function comment: {e}")
            return False

    def get_comment(self, address: int) -> str | None:
        """Get the comment at a specific address.

        Args:
            address: The address to get the comment from

        Returns:
            The comment text if found, None otherwise
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            if not self._current_view.is_valid_offset(address):
                bn.log_error(f"Invalid address for comment: {hex(address)}")
                return None

            comment = self._current_view.get_comment_at(address)
            return comment if comment else None
        except Exception as e:
            bn.log_error(f"Failed to get comment: {e}")
            return None

    def get_function_comment(self, identifier: str | int) -> str | None:
        """Get the comment for a function.

        Args:
            identifier: Function name or address

        Returns:
            The comment text if found, None otherwise
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            func = self.get_function_by_name_or_address(identifier)
            if not func:
                bn.log_error(f"Function not found: {identifier}")
                return None

            comment = self._current_view.get_comment_at(func.start)
            return comment if comment else None
        except Exception as e:
            bn.log_error(f"Failed to get function comment: {e}")
            return None

    def delete_comment(self, address: int) -> bool:
        """Delete a comment at a specific address"""
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            if self._current_view.is_valid_offset(address):
                self._current_view.set_comment_at(address, None)
                return True
        except Exception as e:
            bn.log_error(f"Failed to delete comment: {e}")
        return False

    def delete_function_comment(self, identifier: str | int) -> bool:
        """Delete a comment for a function"""
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            func = self.get_function_by_name_or_address(identifier)
            if not func:
                return False

            self._current_view.set_comment_at(func.start, None)
            return True
        except Exception as e:
            bn.log_error(f"Failed to delete function comment: {e}")
        return False

    # set_integer_display removed per request

    def get_assembly_function(self, identifier: str | int) -> str | None:
        """Get the assembly representation of a function with practical annotations.

        Args:
            identifier: Function name or address

        Returns:
            Assembly code as string, or None if the function cannot be found
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            func = self.get_function_by_name_or_address(identifier)
            if not func:
                bn.log_error(f"Function not found: {identifier}")
                return None

            bn.log_info(f"Found function: {func.name} at {hex(func.start)}")

            var_map = {}  # TODO: Implement this functionality (issues with var.storage not returning the correst sp offset)
            assembly_blocks = {}

            if not hasattr(func, "basic_blocks") or not func.basic_blocks:
                bn.log_error(f"Function {func.name} has no basic blocks")
                # Try alternate approach with linear disassembly
                start_addr = func.start
                try:
                    func_length = func.total_bytes
                    if func_length <= 0:
                        func_length = 1024  # Use a reasonable default if length not available
                except Exception:
                    func_length = 1024  # Use a reasonable default if error

                try:
                    # Create one big block for the entire function
                    block_lines = []
                    current_addr = start_addr
                    end_addr = start_addr + func_length

                    while current_addr < end_addr:
                        try:
                            # Get instruction length
                            instr_len = self._current_view.get_instruction_length(current_addr)
                            if instr_len <= 0:
                                instr_len = 4  # Default to a reasonable instruction length

                            # Get disassembly for this instruction
                            line = self._get_instruction_with_annotations(
                                current_addr, instr_len, var_map
                            )
                            if line:
                                block_lines.append(line)

                            current_addr += instr_len
                        except Exception as e:
                            bn.log_error(f"Error processing address {hex(current_addr)}: {e!s}")
                            block_lines.append(f"# Error at {hex(current_addr)}: {e!s}")
                            current_addr += 1  # Skip to next byte

                    assembly_blocks[start_addr] = [
                        f"# Block at {hex(start_addr)}",
                        *block_lines,
                        "",
                    ]

                except Exception as e:
                    bn.log_error(f"Linear disassembly failed: {e!s}")
                    return None
            else:
                for i, block in enumerate(func.basic_blocks):
                    try:
                        block_lines = []

                        # Process each address in the block
                        addr = block.start
                        while addr < block.end:
                            try:
                                instr_len = self._current_view.get_instruction_length(addr)
                                if instr_len <= 0:
                                    instr_len = 4  # Default to a reasonable instruction length

                                # Get disassembly for this instruction
                                line = self._get_instruction_with_annotations(
                                    addr, instr_len, var_map
                                )
                                if line:
                                    block_lines.append(line)

                                addr += instr_len
                            except Exception as e:
                                bn.log_error(f"Error processing address {hex(addr)}: {e!s}")
                                block_lines.append(f"# Error at {hex(addr)}: {e!s}")
                                addr += 1  # Skip to next byte

                        # Store block with its starting address as key
                        assembly_blocks[block.start] = [
                            f"# Block {i + 1} at {hex(block.start)}",
                            *block_lines,
                            "",
                        ]

                    except Exception as e:
                        bn.log_error(f"Error processing block {i + 1} at {hex(block.start)}: {e!s}")
                        assembly_blocks[block.start] = [
                            f"# Error processing block {i + 1} at {hex(block.start)}: {e!s}",
                            "",
                        ]

            # Sort blocks by address and concatenate them
            sorted_blocks = []
            for addr in sorted(assembly_blocks.keys()):
                sorted_blocks.extend(assembly_blocks[addr])

            return "\n".join(sorted_blocks)
        except Exception as e:
            bn.log_error(f"Error getting assembly for function {identifier}: {e!s}")
            import traceback

            bn.log_error(traceback.format_exc())
            return None

    def _get_instruction_with_annotations(
        self, addr: int, instr_len: int, var_map: dict[int, str]
    ) -> str | None:
        """Get a single instruction with practical annotations.

        Args:
            addr: Address of the instruction
            instr_len: Length of the instruction
            var_map: Dictionary mapping offsets to variable names

        Returns:
            Formatted instruction string with annotations
        """
        if not self._current_view:
            return None

        try:
            # Get raw bytes for fallback
            try:
                raw_bytes = self._current_view.read(addr, instr_len)
                hex_bytes = " ".join(f"{b:02x}" for b in raw_bytes)
            except Exception:
                hex_bytes = "??"

            # Get basic disassembly
            disasm_text = ""
            try:
                if hasattr(self._current_view, "get_disassembly"):
                    disasm = self._current_view.get_disassembly(addr)
                    if disasm:
                        disasm_text = disasm
            except Exception:
                disasm_text = hex_bytes + " ; [Raw bytes]"

            if not disasm_text:
                disasm_text = hex_bytes + " ; [Raw bytes]"

            # Check if this is a call instruction and try to get target function name
            if "call" in disasm_text.lower():
                try:
                    # Extract the address from the call instruction
                    import re

                    addr_pattern = r"0x[0-9a-fA-F]+"
                    match = re.search(addr_pattern, disasm_text)
                    if match:
                        call_addr_str = match.group(0)
                        call_addr = int(call_addr_str, 16)

                        # Look up the target function name
                        sym = self._current_view.get_symbol_at(call_addr)
                        if sym and hasattr(sym, "name"):
                            # Replace the address with the function name
                            disasm_text = disasm_text.replace(call_addr_str, sym.name)
                except Exception:
                    pass

            # Try to annotate memory references with variable names
            try:
                # Look for memory references like [reg+offset]
                import re

                mem_ref_pattern = r"\[([^\]]+)\]"
                mem_refs = re.findall(mem_ref_pattern, disasm_text)

                # For each memory reference, check if it's a known variable
                for mem_ref in mem_refs:
                    # Parse for ebp relative references
                    offset_pattern = r"(ebp|rbp)(([+-]0x[0-9a-fA-F]+)|([+-]\d+))"
                    offset_match = re.search(offset_pattern, mem_ref)
                    if offset_match:
                        # Extract base register and offset
                        offset_match.group(1)
                        offset_str = offset_match.group(2)

                        # Convert offset to integer
                        try:
                            offset = (
                                int(offset_str, 16)
                                if offset_str.startswith("0x") or offset_str.startswith("-0x")
                                else int(offset_str)
                            )

                            # Try to find variable name
                            var_name = var_map.get(offset)

                            # If found, add it to the memory reference
                            if var_name:
                                old_ref = f"[{mem_ref}]"
                                new_ref = f"[{mem_ref} {{{var_name}}}]"
                                disasm_text = disasm_text.replace(old_ref, new_ref)
                        except Exception:
                            pass
            except Exception:
                pass

            # Get comment if any
            comment = None
            try:
                comment = self._current_view.get_comment_at(addr)
            except Exception:
                pass

            # Format the final line
            addr_str = f"{addr:08x}"
            # Include hex bytes column padded for readability
            bytes_col = f"{hex_bytes}".ljust(16)
            line = f"{addr_str}  {bytes_col} {disasm_text}"

            # Add comment at the end if any
            if comment:
                line += f"  ; {comment}"

            return line
        except Exception as e:
            bn.log_error(f"Error annotating instruction at {hex(addr)}: {e!s}")
            return f"{addr:08x}  {hex_bytes} ; [Error: {e!s}]"

    def get_functions_containing_address(self, address: int) -> list:
        """Get functions containing a specific address.

        Args:
            address: The instruction address to find containing functions for

        Returns:
            List of function names containing the address
        """
        if not self.current_view:
            raise RuntimeError("No binary loaded")

        try:
            functions = list(self.current_view.get_functions_containing(address))
            return [func.name for func in functions]
        except Exception as e:
            bn.log_error(f"Error getting functions containing address {hex(address)}: {e}")
            return []

    def get_entry_points(self) -> list[dict[str, Any]]:
        """Return entry point(s) for the current binary view.

        Primarily uses `bv.entry_point`. Also includes common startup symbols like
        `_start` when resolvable.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        bv = self._current_view
        results: list[dict[str, Any]] = []

        def _append(addr: int):
            try:
                if addr is None:
                    return
                name = None
                try:
                    sym = bv.get_symbol_at(addr)
                    if sym and getattr(sym, "name", None):
                        name = sym.name
                except Exception:
                    pass
                if name is None:
                    try:
                        func = bv.get_function_at(addr)
                        if func and getattr(func, "name", None):
                            name = func.name
                    except Exception:
                        pass
                results.append(
                    {
                        "address": hex(int(addr)),
                        "name": name,
                    }
                )
            except Exception:
                pass

        # Primary entry point
        try:
            ep = getattr(bv, "entry_point", None)
            if isinstance(ep, int) and ep >= 0:
                _append(ep)
        except Exception:
            pass

        # Common startup symbol fallback
        for sname in ("_start", "entry", "start", "WinMain", "mainCRTStartup"):
            try:
                sym = bv.get_symbol_by_name(sname) if hasattr(bv, "get_symbol_by_name") else None
                if sym and hasattr(sym, "address"):
                    addr = int(sym.address)
                    if not any(r.get("address") == hex(addr) for r in results):
                        _append(addr)
            except Exception:
                continue

        return results

    # Removed: get_function_code_references() in favor of address-based get_xrefs_to_* helpers

    def get_user_defined_type(self, type_name: str) -> dict[str, Any] | None:
        """Get the definition of a user-defined type (struct, enum, etc.)

        Args:
            type_name: Name of the user-defined type to retrieve

        Returns:
            Dictionary with type information and definition, or None if not found
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            # Check if we have a user type container
            if (
                not hasattr(self._current_view, "user_type_container")
                or not self._current_view.user_type_container
            ):
                bn.log_info("No user type container available")
                return None

            # Search for the requested type by name
            found_type = None
            found_type_id = None

            for type_id in self._current_view.user_type_container.types.keys():
                current_type = self._current_view.user_type_container.types[type_id]
                type_name_from_container = current_type[0]

                if type_name_from_container == type_name:
                    found_type = current_type
                    found_type_id = type_id
                    break

            if not found_type or not found_type_id:
                bn.log_info(f"Type not found: {type_name}")
                return None

            # Determine the type category (struct, enum, etc.)
            type_category = "unknown"
            type_object = found_type[1]
            bn.log_info("Stage1")
            bn.log_info(f"Stage1.5 {type_object.type_class} {StructureVariant.StructStructureType}")
            if type_object.type_class == TypeClass.EnumerationTypeClass:
                type_category = "enum"
            elif type_object.type_class == TypeClass.StructureTypeClass:
                if type_object.type == StructureVariant.StructStructureType:
                    type_category = "struct"
                elif type_object.type == StructureVariant.UnionStructureType:
                    type_category = "union"
                elif type_object.type == StructureVariant.ClassStructureType:
                    type_category = "class"
            elif type_object.type_class == TypeClass.NamedTypeReferenceClass:
                type_category = "typedef"

            # Generate the C++ style definition
            definition_lines = []

            try:
                if (
                    type_category == "struct"
                    or type_category == "class"
                    or type_category == "union"
                ):
                    definition_lines.append(f"{type_category} {type_name} {{")
                    for member in type_object.members:
                        if hasattr(member, "name") and hasattr(member, "type"):
                            definition_lines.append(f"    {member.type} {member.name};")
                    definition_lines.append("};")
                elif type_category == "enum":
                    definition_lines.append(f"enum {type_name} {{")
                    for member in type_object.members:
                        if hasattr(member, "name") and hasattr(member, "value"):
                            definition_lines.append(f"    {member.name} = {member.value},")
                    definition_lines.append("};")
                elif type_category == "typedef":
                    str_type_object = str(type_object)
                    definition_lines.append(f"typedef {str_type_object};")
            except Exception as e:
                bn.log_error(f"Error getting type lines: {e}")

            # Construct the final definition string
            definition = "\n".join(definition_lines)

            return {"name": type_name, "type": type_category, "definition": definition}
        except Exception as e:
            bn.log_error(f"Error getting user-defined type {type_name}: {e}")
            return None

    def get_xrefs_to_address(self, address: int | str) -> dict[str, Any]:
        """Get all cross references (code and data) to a given address.

        Args:
            address: Address as int, hex string (e.g., "0x401000"), or decimal string

        Returns:
            Dictionary with address, code_references, and data_references lists
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        # Normalize address to int
        try:
            if isinstance(address, str):
                addr = int(address, 16) if address.startswith("0x") else int(address)
            else:
                addr = int(address)
        except (TypeError, ValueError):
            raise ValueError("Invalid address format; use hex (0x...) or decimal")

        result: dict[str, Any] = {
            "address": hex(addr),
            "code_references": [],
            "data_references": [],
        }

        # Code references
        try:
            if hasattr(self._current_view, "get_code_refs"):
                for ref in list(self._current_view.get_code_refs(addr)):
                    try:
                        fn_name = ref.function.name if getattr(ref, "function", None) else None
                        entry = {"function": fn_name, "address": hex(ref.address)}

                        # Heuristic: only attach a following call if the referenced data
                        # is carried in a parameter register up to that call (likely passed as an arg)
                        try:
                            func = (
                                ref.function
                                if getattr(ref, "function", None)
                                else self._current_view.get_function_at(ref.address)
                            )
                            if func is not None:
                                import re as _re

                                # identify destination register at xref instruction
                                def _canon_reg(r: str) -> str:
                                    r = (r or "").strip().lower()
                                    mp = {
                                        "rcx": "rcx",
                                        "ecx": "rcx",
                                        "cx": "rcx",
                                        "cl": "rcx",
                                        "ch": "rcx",
                                        "rdx": "rdx",
                                        "edx": "rdx",
                                        "dx": "rdx",
                                        "dl": "rdx",
                                        "dh": "rdx",
                                        "r8": "r8",
                                        "r8d": "r8",
                                        "r8w": "r8",
                                        "r8b": "r8",
                                        "r9": "r9",
                                        "r9d": "r9",
                                        "r9w": "r9",
                                        "r9b": "r9",
                                        "rdi": "rdi",
                                        "edi": "rdi",
                                        "di": "rdi",
                                        "dil": "rdi",
                                        "rsi": "rsi",
                                        "esi": "rsi",
                                        "si": "rsi",
                                        "sil": "rsi",
                                    }
                                    return mp.get(r, r)

                                def _first_op_reg(d: str) -> str:
                                    try:
                                        parts = d.strip().split(None, 1)
                                        if len(parts) < 2:
                                            return ""
                                        ops = parts[1].split(";", 1)[0]
                                        first = ops.split(",", 1)[0].strip()
                                        if "[" in first:
                                            return ""
                                        for kw in ("byte", "word", "dword", "qword", "ptr"):
                                            if first.startswith(kw):
                                                first = first[len(kw) :].strip()
                                        return first.split()[0]
                                    except Exception:
                                        return ""

                                try:
                                    xdis = self._current_view.get_disassembly(ref.address) or ""
                                except Exception:
                                    xdis = ""
                                dest = _canon_reg(_first_op_reg(xdis))
                                arg_regs = {"rcx", "rdx", "r8", "r9", "rdi", "rsi"}
                                if dest in arg_regs:
                                    steps = 16
                                    curr = ref.address
                                    overwritten = False
                                    while steps > 0 and curr < getattr(
                                        func, "highest_address", curr + 1024
                                    ):
                                        ilen = self._current_view.get_instruction_length(curr) or 1
                                        try:
                                            dis = self._current_view.get_disassembly(curr) or ""
                                        except Exception:
                                            dis = ""
                                        # detect clobber of the arg register
                                        if (
                                            curr != ref.address
                                            and _canon_reg(_first_op_reg(dis)) == dest
                                        ):
                                            overwritten = True
                                        if ("call" in dis.lower()) and not overwritten:
                                            entry["following_call_address"] = hex(curr)
                                            m = _re.search(r"0x[0-9a-fA-F]+", dis)
                                            tgt = None
                                            if m:
                                                try:
                                                    tgt = int(m.group(0), 16)
                                                except Exception:
                                                    tgt = None
                                            if tgt is not None:
                                                sym = self._current_view.get_symbol_at(tgt)
                                                if sym and hasattr(sym, "name"):
                                                    entry["following_call_target"] = sym.name
                                                else:
                                                    tfn = self._current_view.get_function_at(tgt)
                                                    entry["following_call_target"] = (
                                                        tfn.name
                                                        if (tfn and hasattr(tfn, "name"))
                                                        else hex(tgt)
                                                    )
                                            break
                                        curr += max(1, ilen)
                                        steps -= 1
                        except Exception:
                            pass

                        result["code_references"].append(entry)
                    except Exception:
                        continue
        except Exception as e:
            bn.log_error(f"Error getting code references to {hex(addr)}: {e}")

        # Data references
        try:
            if hasattr(self._current_view, "get_data_refs"):
                for ref_addr in list(self._current_view.get_data_refs(addr)):
                    try:
                        fn = self._current_view.get_function_at(ref_addr)
                        fn_name = fn.name if fn else None
                        result["data_references"].append(
                            {"function": fn_name, "address": hex(ref_addr)}
                        )
                    except Exception:
                        continue
        except Exception as e:
            bn.log_error(f"Error getting data references to {hex(addr)}: {e}")

        return result

    def get_xrefs_to_field(self, struct_name: str, field_name: str) -> list[dict[str, Any]]:
        """Get all cross references to a named struct field (member).

        This uses a best-effort heuristic:
        - Scans HLIL for occurrences of the field name (e.g., ".field" or "->field")
        - If a global instance of the struct is found, computes the field's absolute
          address (base + offset) and includes code refs to that address
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        struct_name = str(struct_name).strip()
        field_name = str(field_name).strip()
        results: list[dict[str, Any]] = []

        # Try to resolve struct member offset
        member_offset = None
        try:
            if hasattr(self._current_view, "types") and self._current_view.types:
                for t in self._current_view.types.values():
                    try:
                        if (
                            getattr(t, "name", None) == struct_name
                            and hasattr(t, "structure")
                            and t.structure
                        ):
                            for m in getattr(t, "members", getattr(t.structure, "members", [])):
                                if getattr(m, "name", None) == field_name and hasattr(m, "offset"):
                                    member_offset = int(m.offset)
                                    break
                            if member_offset is not None:
                                break
                    except Exception:
                        continue
        except Exception:
            pass

        # HLIL scan for textual member access
        import re

        pattern = re.compile(rf"(\.|->)\s*{re.escape(field_name)}(\b|\W)")
        for func in list(self._current_view.functions):
            try:
                if not hasattr(func, "hlil") or not func.hlil:
                    continue
                for ins in func.hlil.instructions:
                    try:
                        text = str(ins)
                        if pattern.search(text):
                            results.append(
                                {
                                    "kind": "hlil-match",
                                    "function": func.name,
                                    "address": hex(getattr(ins, "address", func.start)),
                                    "text": text,
                                }
                            )
                    except Exception:
                        continue
            except Exception:
                continue

        # If we know the member offset, try to find global instances and code-refs
        if member_offset is not None:
            try:
                for var_addr in list(self._current_view.data_vars):
                    try:
                        t = None
                        if hasattr(self._current_view, "get_type_at"):
                            t = self._current_view.get_type_at(var_addr)
                        t_str = str(t) if t is not None else ""
                        # crude match for exact or pointer to struct
                        if (
                            t_str == struct_name
                            or t_str.endswith(f"* {struct_name}")
                            or struct_name in t_str
                        ):
                            field_addr = var_addr + member_offset
                            # code refs to this absolute address
                            try:
                                for ref in list(self._current_view.get_code_refs(field_addr)):
                                    fn_name = (
                                        ref.function.name
                                        if getattr(ref, "function", None)
                                        else None
                                    )
                                    results.append(
                                        {
                                            "kind": "global-field-ref",
                                            "function": fn_name,
                                            "address": hex(ref.address),
                                            "field_address": hex(field_addr),
                                        }
                                    )
                            except Exception:
                                pass
                    except Exception:
                        continue
            except Exception:
                pass

        return results

    def get_xrefs_to_type(self, type_name: str) -> dict[str, Any]:
        """Get cross references/usages related to a struct/type name.

        Best-effort heuristics:
        - Finds global data variables whose type string mentions the type name; includes code refs to those globals
        - Scans HLIL text for instructions mentioning the type (casts/annotations)
        - Marks functions whose signature mentions the type
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        type_name = str(type_name).strip()
        tnl = type_name.lower()

        result: dict[str, Any] = {
            "type": type_name,
            "data_instances": [],  # [{address, type, name?}]
            "data_code_references": [],  # [{function, address, target}]
            "code_references": [],  # HLIL matches [{function, address, text}]
            "functions_with_type": [],  # function names
        }

        # 1) Global data variables whose type matches the type name
        try:
            for var_addr in list(self._current_view.data_vars):
                try:
                    t = None
                    if hasattr(self._current_view, "get_type_at"):
                        t = self._current_view.get_type_at(var_addr)
                    t_str = str(t) if t is not None else ""
                    if t_str and tnl in t_str.lower():
                        sym = self._current_view.get_symbol_at(var_addr)
                        result["data_instances"].append(
                            {
                                "address": hex(var_addr),
                                "type": t_str,
                                "name": sym.name if sym else None,
                            }
                        )
                        # Also add code refs to this global
                        try:
                            if hasattr(self._current_view, "get_code_refs"):
                                for ref in list(self._current_view.get_code_refs(var_addr)):
                                    fn_name = (
                                        ref.function.name
                                        if getattr(ref, "function", None)
                                        else None
                                    )
                                    result["data_code_references"].append(
                                        {
                                            "function": fn_name,
                                            "address": hex(ref.address),
                                            "target": hex(var_addr),
                                        }
                                    )
                        except Exception:
                            pass
                except Exception:
                    continue
        except Exception:
            pass

        # 2) HLIL textual matches for the type (casts/annotations)
        try:
            import re

            # Look for the type name as a word or part of a cast/annotation
            pat = re.compile(re.escape(type_name), re.IGNORECASE)
            for func in list(self._current_view.functions):
                try:
                    if hasattr(func, "hlil") and func.hlil:
                        for ins in func.hlil.instructions:
                            try:
                                text = str(ins)
                                if pat.search(text):
                                    result["code_references"].append(
                                        {
                                            "function": func.name,
                                            "address": hex(getattr(ins, "address", func.start)),
                                            "text": text,
                                        }
                                    )
                            except Exception:
                                continue
                    # 3) Functions whose signature mentions the type
                    try:
                        sig_text = str(func.type)
                        if sig_text and tnl in sig_text.lower():
                            result["functions_with_type"].append(func.name)
                    except Exception:
                        pass
                except Exception:
                    continue
        except Exception:
            pass

        # Deduplicate function list
        try:
            result["functions_with_type"] = sorted(list(set(result["functions_with_type"])))
        except Exception:
            pass

        return result

    def get_xrefs_to_enum(self, enum_name: str) -> dict[str, Any]:
        """Find usages of an enum by matching its member values in code and variables.

        Notes:
        - Enums are values, not addresses; there are no traditional "data references" to enums.
        - This scans for immediate constants equal to enum members and common bitmask checks.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        enum_name_str = str(enum_name).strip()
        en_lower = enum_name_str.lower()

        result: dict[str, Any] = {
            "enum": enum_name_str,
            "members": [],  # [{name, value}]
            "usages": [],  # [{function, address, text, member, value}]
        }

        # Locate the enum type and collect members
        enum_type = None
        try:
            for t in self._current_view.types.values():
                try:
                    # Match by exact name or case-insensitive
                    if getattr(t, "type_class", None) == TypeClass.EnumerationTypeClass:
                        tname = getattr(t, "name", None)
                        if tname and tname.lower() == en_lower:
                            enum_type = t
                            break
                except Exception:
                    continue
        except Exception:
            pass

        # If not found by exact name, try substring match
        if enum_type is None:
            try:
                for t in self._current_view.types.values():
                    try:
                        if getattr(t, "type_class", None) == TypeClass.EnumerationTypeClass:
                            tname = getattr(t, "name", "")
                            if tname and en_lower in tname.lower():
                                enum_type = t
                                break
                    except Exception:
                        continue
            except Exception:
                pass

        members: list[dict[str, Any]] = []
        values: list[int] = []
        if enum_type is not None:
            try:
                for m in getattr(enum_type, "members", []):
                    try:
                        name = getattr(m, "name", None)
                        val = getattr(m, "value", None)
                        if name is not None and isinstance(val, int):
                            members.append({"name": name, "value": val})
                            values.append(val)
                    except Exception:
                        continue
            except Exception:
                pass

        result["members"] = members

        # Build simple patterns for HLIL text matching of constants (hex)
        import re

        hex_patterns = []
        for v in values:
            hex_patterns.append(re.compile(rf"0x{v:x}\b", re.IGNORECASE))
        # Also a single combined pattern to speed up
        combined_hex = None
        if values:
            combined_hex = re.compile(
                r"(" + "|".join([rf"0x{v:x}\b" for v in values]) + ")", re.IGNORECASE
            )

        # Scan functions for matches
        for func in list(self._current_view.functions):
            try:
                if hasattr(func, "hlil") and func.hlil:
                    for ins in func.hlil.instructions:
                        try:
                            text = str(ins)
                            matched_val = None
                            if combined_hex is not None:
                                m = combined_hex.search(text)
                                if m:
                                    # parse the matched hex back to int to map member name
                                    try:
                                        matched_val = int(m.group(0), 16)
                                    except Exception:
                                        matched_val = None
                            if matched_val is not None:
                                member_name = None
                                for mem in members:
                                    if mem["value"] == matched_val:
                                        member_name = mem["name"]
                                        break
                                result["usages"].append(
                                    {
                                        "function": func.name,
                                        "address": hex(getattr(ins, "address", func.start)),
                                        "text": text,
                                        "member": member_name,
                                        "value": matched_val,
                                    }
                                )
                        except Exception:
                            continue
            except Exception:
                continue

        return result

    def get_xrefs_to_struct(self, struct_name: str) -> dict[str, Any]:
        """Get cross references/usages related specifically to a struct name.

        Includes:
        - members: list of struct members with offsets and types
        - data_instances: globals whose type mentions the struct
        - data_code_references: code refs to those globals
        - field_code_references: code refs to addresses of global_instance + member offset
        - code_references: HLIL lines with member access (".field"/"->field")
        - functions_with_type: functions whose signatures mention the struct
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        name = str(struct_name).strip()
        name_l = name.lower()
        # Build candidate names to handle common PE struct aliases
        candidate_names = {name}
        # Remove leading underscore variant
        if name.startswith("_"):
            candidate_names.add(name[1:])
        else:
            candidate_names.add("_" + name)
        # PE-specific heuristics
        nl = name_l
        if "coff" in nl and "header" in nl:
            candidate_names.update({"IMAGE_FILE_HEADER", "_IMAGE_FILE_HEADER"})
        if ("pe64" in nl or "optional_header64" in nl or "optional" in nl) and "header" in nl:
            candidate_names.update({"IMAGE_OPTIONAL_HEADER64", "_IMAGE_OPTIONAL_HEADER64"})
        if (
            "pe32" in nl or "optional_header32" in nl or ("optional" in nl and "64" not in nl)
        ) and "header" in nl:
            candidate_names.update({"IMAGE_OPTIONAL_HEADER32", "_IMAGE_OPTIONAL_HEADER32"})
        if "dos" in nl and "header" in nl:
            candidate_names.update({"IMAGE_DOS_HEADER", "_IMAGE_DOS_HEADER"})
        candidate_names_l = {c.lower() for c in candidate_names}

        out: dict[str, Any] = {
            "struct": name,
            "members": [],
            "data_instances": [],
            "data_code_references": [],
            "field_code_references": [],
            "code_references": [],
            "functions_with_type": [],
            "vars_with_type": [],
            "code_references_by_cast": [],
        }

        # Resolve the struct type and members
        members = []
        try:
            for t in self._current_view.types.values():
                try:
                    if getattr(t, "type_class", None) == TypeClass.StructureTypeClass:
                        tname = getattr(t, "name", None)
                        if not tname:
                            continue
                        tl = tname.lower()
                        if tl == name_l or name_l in tl or tl in candidate_names_l:
                            for m in getattr(
                                t, "members", getattr(getattr(t, "structure", None), "members", [])
                            ):
                                try:
                                    members.append(
                                        {
                                            "name": getattr(m, "name", None),
                                            "offset": int(getattr(m, "offset", 0))
                                            if hasattr(m, "offset")
                                            else None,
                                            "type": str(getattr(m, "type", ""))
                                            if hasattr(m, "type")
                                            else None,
                                        }
                                    )
                                except Exception:
                                    continue
                            break
                except Exception:
                    continue
        except Exception:
            pass
        out["members"] = members

        # Gather globals with this struct in their type string
        global_instances: list[int] = []
        try:
            for var_addr in list(self._current_view.data_vars):
                try:
                    t = None
                    if hasattr(self._current_view, "get_type_at"):
                        t = self._current_view.get_type_at(var_addr)
                    t_str = str(t) if t is not None else ""
                    if t_str:
                        tl = t_str.lower()
                        if name_l in tl or any(cn in tl for cn in candidate_names_l):
                            sym = self._current_view.get_symbol_at(var_addr)
                            out["data_instances"].append(
                                {
                                    "address": hex(var_addr),
                                    "type": t_str,
                                    "name": sym.name if sym else None,
                                }
                            )
                            global_instances.append(var_addr)
                            # Code refs to the variable itself
                        try:
                            if hasattr(self._current_view, "get_code_refs"):
                                for ref in list(self._current_view.get_code_refs(var_addr)):
                                    fn_name = (
                                        ref.function.name
                                        if getattr(ref, "function", None)
                                        else None
                                    )
                                    out["data_code_references"].append(
                                        {
                                            "function": fn_name,
                                            "address": hex(ref.address),
                                            "target": hex(var_addr),
                                        }
                                    )
                        except Exception:
                            pass
                except Exception:
                    continue
        except Exception:
            pass

        # Also gather symbol-based instances whose name mentions the struct alias
        symbol_instances: list[int] = []
        try:
            for sym in list(self._current_view.get_symbols()):
                try:
                    sname = getattr(sym, "name", "") or ""
                    sfull = getattr(sym, "full_name", "") or ""
                    sl = (sname + " " + sfull).lower()
                    if any(cn in sl for cn in candidate_names_l):
                        addr = getattr(sym, "address", None)
                        if isinstance(addr, int):
                            # capture as data instance if not already present
                            out["data_instances"].append(
                                {
                                    "address": hex(addr),
                                    "type": None,
                                    "name": sname,
                                }
                            )
                            symbol_instances.append(addr)
                            # code refs to this symbol
                            try:
                                if hasattr(self._current_view, "get_code_refs"):
                                    for ref in list(self._current_view.get_code_refs(addr)):
                                        fn_name = (
                                            ref.function.name
                                            if getattr(ref, "function", None)
                                            else None
                                        )
                                        out["data_code_references"].append(
                                            {
                                                "function": fn_name,
                                                "address": hex(ref.address),
                                                "target": hex(addr),
                                            }
                                        )
                            except Exception:
                                pass
                except Exception:
                    continue
        except Exception:
            pass

        # Code refs to computed field addresses for each global instance
        if members and (global_instances or symbol_instances):
            try:
                for base in list(set(global_instances + symbol_instances)):
                    for m in members:
                        try:
                            off = m.get("offset")
                            if off is None:
                                continue
                            field_addr = base + int(off)
                            if hasattr(self._current_view, "get_code_refs"):
                                for ref in list(self._current_view.get_code_refs(field_addr)):
                                    fn_name = (
                                        ref.function.name
                                        if getattr(ref, "function", None)
                                        else None
                                    )
                                    out["field_code_references"].append(
                                        {
                                            "function": fn_name,
                                            "address": hex(ref.address),
                                            "field_address": hex(field_addr),
                                            "member": m.get("name"),
                                        }
                                    )
                        except Exception:
                            continue
            except Exception:
                pass

        # If the struct is contained as a field of another struct, try deriving field addresses from parent instances
        try:
            parent_offsets: list[dict[str, Any]] = []
            for t in self._current_view.types.values():
                try:
                    if getattr(t, "type_class", None) == TypeClass.StructureTypeClass:
                        tname = getattr(t, "name", None)
                        if not tname:
                            continue
                        tl = tname.lower()
                        # scan members for types that mention our struct aliases
                        for mem in getattr(
                            t, "members", getattr(getattr(t, "structure", None), "members", [])
                        ):
                            try:
                                mtype = getattr(mem, "type", None)
                                mtype_str = str(mtype) if mtype is not None else ""
                                ml = mtype_str.lower()
                                if ml and (
                                    name_l in ml or any(cn in ml for cn in candidate_names_l)
                                ):
                                    parent_offsets.append(
                                        {
                                            "parent": tname,
                                            "offset": int(getattr(mem, "offset", 0))
                                            if hasattr(mem, "offset")
                                            else None,
                                            "member": getattr(mem, "name", None),
                                        }
                                    )
                            except Exception:
                                continue
                except Exception:
                    continue

            # For each parent type, find instances and compute field address
            for po in parent_offsets:
                poff = po.get("offset")
                if poff is None:
                    continue
                parent_name = po.get("parent")
                try:
                    # scan data variables
                    for var_addr in list(self._current_view.data_vars):
                        try:
                            t = None
                            if hasattr(self._current_view, "get_type_at"):
                                t = self._current_view.get_type_at(var_addr)
                            t_str = str(t) if t is not None else ""
                            if t_str and parent_name and parent_name.lower() in t_str.lower():
                                field_addr = var_addr + poff
                                if hasattr(self._current_view, "get_code_refs"):
                                    for ref in list(self._current_view.get_code_refs(field_addr)):
                                        fn_name = (
                                            ref.function.name
                                            if getattr(ref, "function", None)
                                            else None
                                        )
                                        out["field_code_references"].append(
                                            {
                                                "function": fn_name,
                                                "address": hex(ref.address),
                                                "field_address": hex(field_addr),
                                                "member": po.get("member"),
                                            }
                                        )
                        except Exception:
                            continue
                    # scan symbols with parent type in name
                    for sym in list(self._current_view.get_symbols()):
                        try:
                            sname = getattr(sym, "name", "") or ""
                            sfull = getattr(sym, "full_name", "") or ""
                            sl = (sname + " " + sfull).lower()
                            if parent_name and parent_name.lower() in sl:
                                addr = getattr(sym, "address", None)
                                if isinstance(addr, int):
                                    field_addr = addr + poff
                                    if hasattr(self._current_view, "get_code_refs"):
                                        for ref in list(
                                            self._current_view.get_code_refs(field_addr)
                                        ):
                                            fn_name = (
                                                ref.function.name
                                                if getattr(ref, "function", None)
                                                else None
                                            )
                                            out["field_code_references"].append(
                                                {
                                                    "function": fn_name,
                                                    "address": hex(ref.address),
                                                    "field_address": hex(field_addr),
                                                    "member": po.get("member"),
                                                }
                                            )
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            pass

        # HLIL matches for member access text

        try:
            import re

            patterns = []
            for m in members:
                nm = m.get("name")
                if not nm:
                    continue
                patterns.append(
                    re.compile(rf"(\.|->)\s*{re.escape(str(nm))}(\b|\W)", re.IGNORECASE)
                )

            for func in list(self._current_view.functions):
                try:
                    # Capture variables whose type mentions the struct
                    try:
                        for v in getattr(func, "vars", []):
                            try:
                                vtype = getattr(v, "type", None)
                                vname = getattr(v, "name", None)
                                vtype_str = str(vtype) if vtype is not None else ""
                                if vtype_str and name_l in vtype_str.lower():
                                    out["vars_with_type"].append(
                                        {
                                            "function": func.name,
                                            "var": vname,
                                            "type": vtype_str,
                                        }
                                    )
                            except Exception:
                                continue
                    except Exception:
                        pass

                    if hasattr(func, "hlil") and func.hlil:
                        for ins in func.hlil.instructions:
                            try:
                                text = str(ins)
                                if any(p.search(text) for p in patterns):
                                    out["code_references"].append(
                                        {
                                            "function": func.name,
                                            "address": hex(getattr(ins, "address", func.start)),
                                            "text": text,
                                        }
                                    )
                                # Also capture casts/annotations explicitly mentioning the struct name
                                tl = text.lower()
                                if name_l in tl or any(cn in tl for cn in candidate_names_l):
                                    # Heuristic: detect patterns like '(COFF_Header*)' or '(struct COFF_Header*)'
                                    cast_pat = (
                                        r"\(.*("
                                        + "|".join(re.escape(c) for c in candidate_names)
                                        + r").*\)"
                                    )
                                    if re.search(cast_pat, text, re.IGNORECASE):
                                        out["code_references_by_cast"].append(
                                            {
                                                "function": func.name,
                                                "address": hex(getattr(ins, "address", func.start)),
                                                "text": text,
                                            }
                                        )
                            except Exception:
                                continue
                    # Functions whose signature mentions the struct
                    try:
                        sig_text = str(func.type)
                        if sig_text:
                            sl = sig_text.lower()
                            if name_l in sl or any(cn in sl for cn in candidate_names_l):
                                out["functions_with_type"].append(func.name)
                    except Exception:
                        pass
                except Exception:
                    continue
        except Exception:
            pass

        # Dedup functions list
        try:
            out["functions_with_type"] = sorted(list(set(out["functions_with_type"])))
        except Exception:
            pass

        return out

    def get_xrefs_to_union(self, union_name: str) -> dict[str, Any]:
        """Get cross references/usages related to a union type by name.

        Includes:
        - members: list of union members with offsets/types (offsets may be 0/overlapping)
        - data_instances: globals whose type mentions the union
        - data_code_references: code refs to those globals
        - code_references: HLIL lines with member access (".field"/"->field")
        - functions_with_type: functions whose signatures mention the union
        - vars_with_type: function-local variables typed as the union
        - code_references_by_cast: HLIL lines with explicit casts mentioning the union
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        name = str(union_name).strip()
        name_l = name.lower()

        out: dict[str, Any] = {
            "union": name,
            "members": [],
            "data_instances": [],
            "data_code_references": [],
            "code_references": [],
            "functions_with_type": [],
            "vars_with_type": [],
            "code_references_by_cast": [],
        }

        # Resolve union members
        members: list[dict[str, Any]] = []
        try:
            for t in self._current_view.types.values():
                try:
                    # Union types are presented via StructureTypeClass with UnionStructureType variant
                    if getattr(t, "type_class", None) == TypeClass.StructureTypeClass:
                        tname = getattr(t, "name", None)
                        if not tname:
                            continue
                        tl = tname.lower()
                        if tl == name_l or name_l in tl:
                            # If the BN type exposes a variant, prefer checking for union
                            try:
                                if getattr(t, "type", None) == StructureVariant.UnionStructureType:
                                    pass
                            except Exception:
                                pass
                            for m in getattr(
                                t, "members", getattr(getattr(t, "structure", None), "members", [])
                            ):
                                try:
                                    members.append(
                                        {
                                            "name": getattr(m, "name", None),
                                            "offset": int(getattr(m, "offset", 0))
                                            if hasattr(m, "offset")
                                            else None,
                                            "type": str(getattr(m, "type", ""))
                                            if hasattr(m, "type")
                                            else None,
                                        }
                                    )
                                except Exception:
                                    continue
                            break
                except Exception:
                    continue
        except Exception:
            pass
        out["members"] = members

        # Gather globals with this union in their type string
        try:
            for var_addr in list(self._current_view.data_vars):
                try:
                    t = None
                    if hasattr(self._current_view, "get_type_at"):
                        t = self._current_view.get_type_at(var_addr)
                    t_str = str(t) if t is not None else ""
                    if t_str and name_l in t_str.lower():
                        sym = self._current_view.get_symbol_at(var_addr)
                        out["data_instances"].append(
                            {
                                "address": hex(var_addr),
                                "type": t_str,
                                "name": sym.name if sym else None,
                            }
                        )
                        # Code refs to that variable
                        try:
                            if hasattr(self._current_view, "get_code_refs"):
                                for ref in list(self._current_view.get_code_refs(var_addr)):
                                    fn_name = (
                                        ref.function.name
                                        if getattr(ref, "function", None)
                                        else None
                                    )
                                    out["data_code_references"].append(
                                        {
                                            "function": fn_name,
                                            "address": hex(ref.address),
                                            "target": hex(var_addr),
                                        }
                                    )
                        except Exception:
                            pass
                except Exception:
                    continue
        except Exception:
            pass

        # HLIL member access and casts; function variables/signatures
        try:
            import re

            patterns = []
            for m in members:
                nm = m.get("name")
                if not nm:
                    continue
                patterns.append(
                    re.compile(rf"(\.|->)\s*{re.escape(str(nm))}(\b|\W)", re.IGNORECASE)
                )

            for func in list(self._current_view.functions):
                try:
                    # variables typed as this union
                    try:
                        for v in getattr(func, "vars", []):
                            try:
                                vtype = getattr(v, "type", None)
                                vname = getattr(v, "name", None)
                                vtype_str = str(vtype) if vtype is not None else ""
                                if vtype_str and name_l in vtype_str.lower():
                                    out["vars_with_type"].append(
                                        {
                                            "function": func.name,
                                            "var": vname,
                                            "type": vtype_str,
                                        }
                                    )
                            except Exception:
                                continue
                    except Exception:
                        pass

                    if hasattr(func, "hlil") and func.hlil:
                        for ins in func.hlil.instructions:
                            try:
                                text = str(ins)
                                tl = text.lower()
                                matched_member = (
                                    any(p.search(text) for p in patterns) if patterns else False
                                )
                                if matched_member:
                                    out["code_references"].append(
                                        {
                                            "function": func.name,
                                            "address": hex(getattr(ins, "address", func.start)),
                                            "text": text,
                                        }
                                    )
                                # Capture casts mentioning the union
                                cast_matched = False
                                if name_l in tl:
                                    if re.search(
                                        rf"\(.*{re.escape(name)}.*\)", text, re.IGNORECASE
                                    ):
                                        out["code_references_by_cast"].append(
                                            {
                                                "function": func.name,
                                                "address": hex(getattr(ins, "address", func.start)),
                                                "text": text,
                                            }
                                        )
                                        cast_matched = True
                                # Fallback: any HLIL mention of the union name counts as a code reference
                                if (not matched_member) and (not cast_matched) and (name_l in tl):
                                    out["code_references"].append(
                                        {
                                            "function": func.name,
                                            "address": hex(getattr(ins, "address", func.start)),
                                            "text": text,
                                        }
                                    )
                            except Exception:
                                continue
                    # function signature mentions
                    try:
                        sig_text = str(func.type)
                        if sig_text and name_l in sig_text.lower():
                            out["functions_with_type"].append(func.name)
                    except Exception:
                        pass
                except Exception:
                    continue
        except Exception:
            pass

        # Dedup functions list
        try:
            out["functions_with_type"] = sorted(list(set(out["functions_with_type"])))
        except Exception:
            pass

        return out

    def patch_bytes(
        self, address: str | int, data: str | bytes | list[int], save_to_file: bool = True
    ) -> dict[str, Any]:
        """Patch bytes at a given address in the binary.

        Args:
            address: Address to patch (hex string like "0x401000" or integer)
            data: Bytes to write. Can be:
                - Hex string: "90 90" or "9090" or "0x90 0x90"
                - List of integers: [0x90, 0x90]
                - Bytes object: b"\x90\x90"
            save_to_file: If True (default), save the patched binary to disk

        Returns:
            Dictionary with status, address, original bytes, and patched bytes

        Raises:
            RuntimeError: If no binary is loaded
            ValueError: If address or data format is invalid
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        # Parse address
        # Only treat as hex if it has "0x" prefix or contains a-f/A-F characters
        # This avoids ambiguity where "123" would be treated as hex instead of decimal
        if isinstance(address, str):
            address = address.strip()
            if address.startswith("0x") or address.startswith("0X"):
                addr = int(address, 16)
            elif any(c in "abcdefABCDEF" for c in address):
                # Contains hex letters, treat as hex
                addr = int(address, 16)
            else:
                # Pure digits, treat as decimal
                addr = int(address, 10)
        else:
            addr = int(address)

        # Parse data into bytes
        patch_bytes = None
        if isinstance(data, bytes):
            patch_bytes = data
        elif isinstance(data, str):
            # Try to parse as hex string
            data_str = data.strip()
            # Remove "0x" prefix if present
            if data_str.startswith("0x"):
                data_str = data_str[2:]
            # Remove spaces
            data_str = data_str.replace(" ", "").replace("\n", "").replace("\t", "")
            # Convert hex string to bytes
            try:
                patch_bytes = bytes.fromhex(data_str)
            except ValueError as e:
                raise ValueError(f"Invalid hex string: {e}")
        elif isinstance(data, list):
            # List of integers
            try:
                patch_bytes = bytes(data)
            except (ValueError, TypeError) as e:
                raise ValueError(f"Invalid byte list: {e}")
        else:
            raise ValueError(f"Unsupported data type: {type(data)}")

        if not patch_bytes:
            raise ValueError("Empty patch data")

        # Read original bytes for comparison
        try:
            original_bytes = self._current_view.read(addr, len(patch_bytes))
            if original_bytes is None:
                original_bytes = b""
        except Exception as e:
            bn.log_warn(f"Could not read original bytes at {hex(addr)}: {e}")
            original_bytes = b""

        # Write the patch
        try:
            written = self._current_view.write(addr, patch_bytes)

            # Determine status based on whether all bytes were written
            if written != len(patch_bytes):
                bn.log_warn(f"Only wrote {written} of {len(patch_bytes)} bytes at {hex(addr)}")
                status = "partial"
            else:
                status = "ok"

            result = {
                "status": status,
                "address": hex(addr),
                "original_bytes": original_bytes.hex() if original_bytes else "",
                "patched_bytes": patch_bytes.hex(),
                "bytes_written": written,
                "bytes_requested": len(patch_bytes),
                "saved_to_file": False,
            }

            # Add warning message if partial write
            if status == "partial":
                result["warning"] = f"Only wrote {written} of {len(patch_bytes)} bytes"

            # Save to file if requested
            if save_to_file:
                try:
                    # Get the original file path
                    original_file = self._current_view.file.filename
                    if original_file:
                        # Save the patched binary back to the original file
                        if self._current_view.save(original_file):
                            result["saved_to_file"] = True
                            result["saved_path"] = original_file
                            bn.log_info(f"Patched binary saved to: {original_file}")

                            # On macOS, re-sign the binary to avoid "killed" error
                            if platform.system() == "Darwin":
                                result["codesign"] = self._codesign_binary(original_file)
                        else:
                            bn.log_warn(f"Failed to save patched binary to: {original_file}")
                            result["save_error"] = "save() returned False"
                    else:
                        bn.log_warn("No original file path available for saving")
                        result["save_error"] = "No original file path"
                except Exception as save_e:
                    bn.log_warn(f"Failed to save patched binary: {save_e}")
                    result["save_error"] = str(save_e)

            return result
        except Exception as e:
            raise ValueError(f"Failed to patch bytes at {hex(addr)}: {e!s}")

    def _codesign_binary(self, file_path: str) -> dict[str, Any]:
        """Re-sign a binary on macOS after patching.

        On macOS, modifying a binary invalidates its code signature, causing the
        system to kill the process when executed. This method removes the old
        signature and applies an ad-hoc signature to make the binary executable.

        Args:
            file_path: Path to the binary file to sign

        Returns:
            Dictionary with codesign status and any error messages
        """
        result = {
            "attempted": True,
            "success": False,
            "platform": "macOS",
        }

        try:
            # Step 1: Remove existing signature (optional, codesign -f will overwrite anyway)
            remove_result = subprocess.run(
                ["codesign", "--remove-signature", file_path],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if remove_result.returncode != 0:
                # It's okay if removal fails (binary might not have been signed)
                bn.log_info(
                    f"codesign --remove-signature returned {remove_result.returncode}: {remove_result.stderr}"
                )

            # Step 2: Apply ad-hoc signature with force flag
            sign_result = subprocess.run(
                ["codesign", "-f", "-s", "-", file_path], capture_output=True, text=True, timeout=30
            )

            if sign_result.returncode == 0:
                result["success"] = True
                result["message"] = "Binary re-signed with ad-hoc signature"
                bn.log_info(f"Successfully re-signed binary: {file_path}")
            else:
                result["error"] = (
                    sign_result.stderr or f"codesign failed with code {sign_result.returncode}"
                )
                bn.log_warn(f"Failed to re-sign binary: {result['error']}")

        except FileNotFoundError:
            result["error"] = "codesign command not found"
            bn.log_warn("codesign command not found - is Xcode Command Line Tools installed?")
        except subprocess.TimeoutExpired:
            result["error"] = "codesign command timed out"
            bn.log_warn("codesign command timed out")
        except Exception as e:
            result["error"] = str(e)
            bn.log_warn(f"Error during codesign: {e}")

        return result

    # -------- WoW Emulation Tools --------

    def scan_lua_api_strings(
        self, namespace_filter: str = "", offset: int = 0, limit: int = 100
    ) -> dict:
        """Scan for Lua API 'Usage:' strings and find associated native function pointers.

        Searches for strings starting with 'Usage: ' or matching Lua C API patterns,
        then traces xrefs to find the registering function and the native handler address.

        Returns dict with 'entries' list and 'total' count.
        """
        bv = self._current_view
        if not bv:
            return {"error": "No binary loaded", "entries": [], "total": 0}

        entries = []

        for s in bv.strings:
            val = s.value
            if not val:
                continue
            if not (val.startswith("Usage: ") or ("::" in val and "(" in val)):
                continue

            if namespace_filter and namespace_filter.lower() not in val.lower():
                continue

            entry = {
                "string": val,
                "string_addr": hex(s.start),
                "func_addr": None,
                "func_name": None,
            }

            code_refs = list(bv.get_code_refs(s.start))
            if code_refs:
                ref_func = code_refs[0].function
                if ref_func:
                    entry["func_addr"] = hex(ref_func.start)
                    entry["func_name"] = ref_func.name

            entries.append(entry)

        total = len(entries)
        paginated = entries[offset : offset + limit]

        return {"entries": paginated, "total": total, "offset": offset, "limit": limit}

    def scan_rtti_entries(self, class_filter: str = "", offset: int = 0, limit: int = 100) -> dict:
        """Scan for RTTI type_info entries and their associated vtables.

        Finds MSVC RTTI type_info structures by searching for the '.?AV' mangled name prefix.

        Returns dict with 'entries' list and 'total' count.
        """
        bv = self._current_view
        if not bv:
            return {"error": "No binary loaded", "entries": [], "total": 0}

        entries = []
        is_64bit = bv.arch.name == "x86_64"

        for s in bv.strings:
            val = s.value
            if not val:
                continue
            if not val.startswith(".?A"):
                continue

            if class_filter and class_filter.lower() not in val.lower():
                continue

            demangled = val
            try:
                _, qname = bn.demangle_ms(bv.arch, val)
                if qname:
                    demangled = "::".join(str(part) for part in qname)
            except Exception:
                pass

            entry = {
                "mangled_name": val,
                "class_name": demangled,
                "type_info_addr": None,
                "vtable_addr": None,
                "num_vfuncs": 0,
            }

            name_offset = 0x10 if is_64bit else 0x08
            type_info_addr = s.start - name_offset
            entry["type_info_addr"] = hex(type_info_addr)

            data_refs_to_ti = list(bv.get_data_refs(type_info_addr))
            for ref in data_refs_to_ti:
                col_candidate = ref
                ptr_size = 8 if is_64bit else 4
                vtable_candidate = col_candidate - ptr_size

                vtable_refs = list(bv.get_data_refs_from(vtable_candidate))
                if vtable_refs:
                    func_at = bv.get_function_at(vtable_refs[0])
                    if func_at:
                        entry["vtable_addr"] = hex(vtable_candidate)
                        count = 0
                        addr = vtable_candidate
                        while True:
                            refs = list(bv.get_data_refs_from(addr))
                            if not refs:
                                break
                            seg = bv.get_segment_at(refs[0])
                            if not seg or not seg.executable:
                                break
                            count += 1
                            addr += ptr_size
                        entry["num_vfuncs"] = count
                        break

            entries.append(entry)

        total = len(entries)
        paginated = entries[offset : offset + limit]

        return {"entries": paginated, "total": total, "offset": offset, "limit": limit}

    def scan_update_fields(self, object_type: str = "", offset: int = 0, limit: int = 100) -> dict:
        """Scan for WoW update field strings (CG*Data:: patterns) and their handler functions.

        Returns dict with 'entries' list and 'total' count.
        """
        bv = self._current_view
        if not bv:
            return {"error": "No binary loaded", "entries": [], "total": 0}

        entries = []

        for s in bv.strings:
            val = s.value
            if not val:
                continue
            if not (val.startswith("CG") and "Data::" in val):
                continue

            if object_type and object_type.lower() not in val.lower():
                continue

            entry = {
                "field_name": val,
                "string_addr": hex(s.start),
                "handler_addr": None,
                "handler_name": None,
            }

            code_refs = list(bv.get_code_refs(s.start))
            if code_refs:
                ref_func = code_refs[0].function
                if ref_func:
                    entry["handler_addr"] = hex(ref_func.start)
                    entry["handler_name"] = ref_func.name

            entries.append(entry)

        total = len(entries)
        paginated = entries[offset : offset + limit]

        return {"entries": paginated, "total": total, "offset": offset, "limit": limit}

    def batch_rename_functions(self, renames: list[dict]) -> dict:
        """Rename multiple functions in a single call.

        Args:
            renames: List of dicts with 'address' (hex string or int) and 'name' fields.

        Returns dict with 'success_count', 'failure_count', and 'results' list.
        """
        bv = self._current_view
        if not bv:
            return {
                "error": "No binary loaded",
                "success_count": 0,
                "failure_count": 0,
                "results": [],
            }

        results = []
        success_count = 0
        failure_count = 0

        for item in renames:
            addr_raw = item.get("address", "")
            name = item.get("name", "")
            result_item = {"address": addr_raw, "name": name}

            try:
                if isinstance(addr_raw, int):
                    addr = addr_raw
                elif isinstance(addr_raw, str):
                    addr_str = addr_raw.strip()
                    if addr_str.lower().startswith("0x"):
                        addr = int(addr_str, 16)
                    else:
                        addr = int(addr_str, 16)
                else:
                    result_item["status"] = "error"
                    result_item["error"] = f"Invalid address type: {type(addr_raw)}"
                    failure_count += 1
                    results.append(result_item)
                    continue

                func = bv.get_function_at(addr)
                if func is None:
                    result_item["status"] = "error"
                    result_item["error"] = f"No function at address {hex(addr)}"
                    failure_count += 1
                else:
                    old_name = func.name
                    func.name = name
                    result_item["status"] = "ok"
                    result_item["old_name"] = old_name
                    success_count += 1

            except Exception as e:
                result_item["status"] = "error"
                result_item["error"] = str(e)
                failure_count += 1

            results.append(result_item)

        return {
            "success_count": success_count,
            "failure_count": failure_count,
            "total": len(renames),
            "results": results,
        }

    def discover_lua_reg_tables(
        self, min_entries: int = 3, offset: int = 0, limit: int = 50
    ) -> dict:
        """Scan .rdata and .data for luaL_Reg-style {char*, func*} registration tables.

        Reads raw section bytes and checks pointer pairs at pointer-width intervals.
        Groups consecutive valid entries into tables, then validates string content.

        Args:
            min_entries: Minimum entries for a run to be reported as a table.
            offset: Pagination offset (on discovered tables list).
            limit: Maximum tables to return.

        Returns:
            dict with 'tables' list, 'total' table count, 'total_entries' entry count.
        """
        bv = self._current_view
        if not bv:
            return {"error": "No binary loaded", "tables": [], "total": 0, "total_entries": 0}

        is_64bit = bv.arch.name == "x86_64"
        ptr_size = 8 if is_64bit else 4
        entry_size = ptr_size * 2
        unpack_fmt = "<QQ" if is_64bit else "<II"

        # Collect section ranges
        rdata_ranges = []
        data_ranges = []
        text_ranges = []
        for name, sec in bv.sections.items():
            if name in (".rdata", ".rodata"):
                rdata_ranges.append((sec.start, sec.end))
            elif name == ".data":
                data_ranges.append((sec.start, sec.end))
            elif name == ".text":
                text_ranges.append((sec.start, sec.end))

        all_data_ranges = rdata_ranges + data_ranges
        if not all_data_ranges or not text_ranges or not rdata_ranges:
            return {"error": "Required sections not found", "tables": [], "total": 0, "total_entries": 0}

        def in_ranges(addr, ranges):
            for start, end in ranges:
                if start <= addr < end:
                    return True
            return False

        def in_rdata(addr):
            return in_ranges(addr, rdata_ranges)

        def in_text(addr):
            return in_ranges(addr, text_ranges)

        def read_string_at(addr, max_len=128):
            """Read a null-terminated ASCII string from the binary."""
            try:
                raw = bv.read(addr, max_len)
                if not raw:
                    return None
                end = raw.find(b'\x00')
                if end < 0:
                    return None
                s = raw[:end]
                try:
                    return s.decode('ascii')
                except UnicodeDecodeError:
                    return None
            except Exception:
                return None

        def is_valid_lua_name(s):
            """Check if a string looks like a valid Lua API function name."""
            if not s or len(s) < 1 or len(s) > 100:
                return False
            if not (s[0].isalpha() or s[0] == '_'):
                return False
            return all(c.isalnum() or c == '_' for c in s)

        # Scan each data section for pointer pairs
        tables = []
        for sec_name, sec in bv.sections.items():
            if sec_name not in (".rdata", ".rodata", ".data"):
                continue

            sec_data = bv.read(sec.start, sec.end - sec.start)
            if not sec_data:
                continue

            sec_len = len(sec_data)
            # Find runs of consecutive valid {rdata_ptr, text_ptr} entries
            current_run = []
            current_run_start = None

            i = 0
            while i + entry_size <= sec_len:
                name_ptr, func_ptr = struct.unpack_from(unpack_fmt, sec_data, i)
                addr = sec.start + i

                if name_ptr != 0 and func_ptr != 0 and in_rdata(name_ptr) and in_text(func_ptr):
                    if not current_run:
                        current_run_start = addr
                    current_run.append((addr, name_ptr, func_ptr))
                else:
                    # Check for NULL terminator {0, 0} which ends a Lua table
                    if current_run and name_ptr == 0 and func_ptr == 0:
                        # NULL terminator is part of the table, finalize
                        pass
                    if current_run and len(current_run) >= min_entries:
                        tables.append((current_run_start, sec_name, list(current_run)))
                    current_run = []
                    current_run_start = None

                i += entry_size

            # Flush remaining run
            if current_run and len(current_run) >= min_entries:
                tables.append((current_run_start, sec_name, list(current_run)))

        # Validate strings and build output
        validated_tables = []
        total_entries = 0

        for table_addr, sec_name, run in tables:
            entries = []
            valid_names = 0
            for _, name_ptr, func_ptr in run:
                name_str = read_string_at(name_ptr)
                if name_str and is_valid_lua_name(name_str):
                    valid_names += 1
                entries.append({
                    "name": name_str if name_str else None,
                    "name_addr": hex(name_ptr),
                    "func_addr": hex(func_ptr),
                })

            # Require >= 80% valid Lua names
            if len(entries) > 0 and (valid_names / len(entries)) >= 0.8:
                validated_tables.append({
                    "table_addr": hex(table_addr),
                    "entry_count": len(entries),
                    "valid_names": valid_names,
                    "section": sec_name,
                    "entries": entries,
                })
                total_entries += len(entries)

        total = len(validated_tables)
        paginated = validated_tables[offset:offset + limit]

        return {
            "tables": paginated,
            "total": total,
            "total_entries": total_entries,
            "offset": offset,
            "limit": limit,
        }

    def walk_rtti_vtables(
        self, class_filter: str = "", offset: int = 0, limit: int = 100
    ) -> dict:
        """Scan data sections for MSVC RTTI COL structures by matching type_info RVAs.

        Reads raw section bytes from .rdata and .data (some binaries store RTTI in
        either section). Finds COL by RVA pattern, then finds vtable by scanning for
        a pointer to the COL address.

        Args:
            class_filter: Substring filter on class name.
            offset: Pagination offset.
            limit: Maximum entries to return.

        Returns:
            dict with 'entries' list and 'total' count.
        """
        bv = self._current_view
        if not bv:
            return {"error": "No binary loaded", "entries": [], "total": 0}

        is_64bit = bv.arch.name == "x86_64"
        ptr_size = 8 if is_64bit else 4

        module_base = bv.start

        # Collect all scannable data sections and .text ranges
        # RTTI structures can be in .rdata or .data depending on the binary
        scan_sections = []  # list of (start_addr, bytes_data)
        text_ranges = []

        for name, sec in bv.sections.items():
            if name in (".rdata", ".rodata", ".data"):
                sec_bytes = bv.read(sec.start, sec.end - sec.start)
                if sec_bytes:
                    scan_sections.append((sec.start, sec_bytes))
            elif name == ".text":
                text_ranges.append((sec.start, sec.end))

        if not scan_sections or not text_ranges:
            return {"error": "Required sections not found", "entries": [], "total": 0}

        def in_text(addr):
            for start, end in text_ranges:
                if start <= addr < end:
                    return True
            return False

        # Step 1: Find all type_info entries
        type_infos = []
        for s in bv.strings:
            val = s.value
            if not val or not val.startswith(".?A"):
                continue
            if class_filter and class_filter.lower() not in val.lower():
                continue

            name_offset = 0x10 if is_64bit else 0x08
            type_info_addr = s.start - name_offset

            demangled = val
            try:
                _, qname = bn.demangle_ms(bv.arch, val)
                if qname:
                    demangled = "::".join(str(part) for part in qname)
            except Exception:
                pass

            type_infos.append((type_info_addr, val, demangled))

        # Step 2: For each type_info, find COL then vtable across all data sections
        entries = []

        for ti_addr, mangled, demangled in type_infos:
            best_vtable = None
            best_vfunc_count = 0
            found_col_addr = None

            if is_64bit:
                # x64 COL: signature(4)=1, offset(4), cdOffset(4),
                #          typeDescRVA(4), classHierRVA(4), selfRVA(4)
                # Total: 24 bytes. typeDescRVA at +12, selfRVA at +20.
                ti_rva = ti_addr - module_base
                ti_rva_bytes = struct.pack("<I", ti_rva & 0xFFFFFFFF)

                # Scan all data sections for COL containing this type_info RVA
                for sec_start, sec_data in scan_sections:
                    sec_len = len(sec_data)
                    search_offset = 0
                    while search_offset < sec_len - 24:
                        pos = sec_data.find(ti_rva_bytes, search_offset)
                        if pos < 0:
                            break
                        search_offset = pos + 4

                        col_offset = pos - 12
                        if col_offset < 0:
                            continue

                        sig = struct.unpack_from("<I", sec_data, col_offset)[0]
                        if sig != 1:
                            continue

                        col_addr = sec_start + col_offset

                        # Verify selfRVA at +20
                        self_rva = struct.unpack_from("<I", sec_data, col_offset + 20)[0]
                        expected_self_rva = (col_addr - module_base) & 0xFFFFFFFF
                        if self_rva != expected_self_rva:
                            continue

                        found_col_addr = col_addr
                        break
                    if found_col_addr:
                        break

                # Find vtable: scan all data sections for an 8-byte VA pointer to COL
                if found_col_addr is not None:
                    col_va_bytes = struct.pack("<Q", found_col_addr)
                    for sec_start, sec_data in scan_sections:
                        sec_len = len(sec_data)
                        vt_search = 0
                        while vt_search < sec_len - ptr_size:
                            vt_pos = sec_data.find(col_va_bytes, vt_search)
                            if vt_pos < 0:
                                break
                            vt_search = vt_pos + ptr_size

                            vtable_file_offset = vt_pos + ptr_size
                            vtable_addr = sec_start + vtable_file_offset

                            # Walk vtable entries
                            count = 0
                            walk_off = vtable_file_offset
                            while walk_off + ptr_size <= sec_len:
                                vfunc = struct.unpack_from("<Q", sec_data, walk_off)[0]
                                if not in_text(vfunc):
                                    break
                                count += 1
                                walk_off += ptr_size

                            if count > best_vfunc_count:
                                best_vfunc_count = count
                                best_vtable = (sec_start, sec_data, vtable_file_offset, vtable_addr)

            else:
                # x86 COL: signature(4)=0, offset(4), cdOffset(4), typeDescPtr(4)
                # typeDescPtr is a VA (not RVA) on x86
                ti_ptr_bytes = struct.pack("<I", ti_addr & 0xFFFFFFFF)

                for sec_start, sec_data in scan_sections:
                    sec_len = len(sec_data)
                    search_offset = 0
                    while search_offset < sec_len - 16:
                        pos = sec_data.find(ti_ptr_bytes, search_offset)
                        if pos < 0:
                            break
                        search_offset = pos + 4

                        col_offset = pos - 12
                        if col_offset < 0:
                            continue

                        sig = struct.unpack_from("<I", sec_data, col_offset)[0]
                        if sig != 0:
                            continue

                        found_col_addr = sec_start + col_offset
                        break
                    if found_col_addr:
                        break

                if found_col_addr is not None:
                    col_ptr_bytes = struct.pack("<I", found_col_addr & 0xFFFFFFFF)
                    for sec_start, sec_data in scan_sections:
                        sec_len = len(sec_data)
                        vt_search = 0
                        while vt_search < sec_len - ptr_size:
                            vt_pos = sec_data.find(col_ptr_bytes, vt_search)
                            if vt_pos < 0:
                                break
                            vt_search = vt_pos + ptr_size

                            vtable_file_offset = vt_pos + ptr_size
                            vtable_addr = sec_start + vtable_file_offset

                            count = 0
                            walk_off = vtable_file_offset
                            while walk_off + ptr_size <= sec_len:
                                vfunc = struct.unpack_from("<I", sec_data, walk_off)[0]
                                if not in_text(vfunc):
                                    break
                                count += 1
                                walk_off += ptr_size

                            if count > best_vfunc_count:
                                best_vfunc_count = count
                                best_vtable = (sec_start, sec_data, vtable_file_offset, vtable_addr)

            if best_vtable is not None:
                _, vt_sec_data, vt_file_off, vtable_addr = best_vtable
                vt_sec_len = len(vt_sec_data)
                vfunc_addrs = []
                for j in range(min(best_vfunc_count, 5)):
                    off = vt_file_off + j * ptr_size
                    if off + ptr_size <= vt_sec_len:
                        fmt = "<Q" if is_64bit else "<I"
                        va = struct.unpack_from(fmt, vt_sec_data, off)[0]
                        vfunc_addrs.append(hex(va))

                entries.append({
                    "class_name": demangled,
                    "mangled_name": mangled,
                    "type_info_addr": hex(ti_addr),
                    "vtable_addr": hex(vtable_addr),
                    "num_vfuncs": best_vfunc_count,
                    "first_vfuncs": vfunc_addrs,
                })
            else:
                entries.append({
                    "class_name": demangled,
                    "mangled_name": mangled,
                    "type_info_addr": hex(ti_addr),
                    "vtable_addr": None,
                    "num_vfuncs": 0,
                    "first_vfuncs": [],
                })

        total = len(entries)
        paginated = entries[offset:offset + limit]

        return {"entries": paginated, "total": total, "offset": offset, "limit": limit}

    def scan_vtables(
        self, min_methods: int = 2, offset: int = 0, limit: int = 100
    ) -> dict:
        """Scan data sections for vtable-like structures: contiguous runs of .text pointers.

        Finds vtables directly by scanning .rdata and .data for consecutive
        pointer-sized values that all point into .text. For each candidate,
        checks vtable[-1] for an MSVC RTTI COL pointer to resolve class names.

        This complements walk_rtti_vtables which requires COL structures to
        exist. Many game binaries have stripped RTTI, so this scanner finds
        vtables that the RTTI-based approach misses.

        Args:
            min_methods: Minimum number of consecutive .text pointers to report.
            offset: Pagination offset.
            limit: Maximum entries to return.

        Returns:
            dict with 'vtables' list, 'total', 'named_count', 'offset', 'limit'.
        """
        bv = self._current_view
        if not bv:
            return {"error": "No binary loaded", "vtables": [], "total": 0, "named_count": 0}

        is_64bit = bv.arch.name == "x86_64"
        ptr_size = 8 if is_64bit else 4
        ptr_fmt = "<Q" if is_64bit else "<I"
        module_base = bv.start

        # Collect .text ranges and data sections
        scan_sections = []  # list of (name, start_addr, bytes_data)
        text_ranges = []
        data_ranges = []  # (start, end) for all data sections

        for name, sec in bv.sections.items():
            if name in (".rdata", ".rodata", ".data"):
                sec_bytes = bv.read(sec.start, sec.end - sec.start)
                if sec_bytes:
                    scan_sections.append((name, sec.start, sec_bytes))
                    data_ranges.append((sec.start, sec.end))
            elif name == ".text":
                text_ranges.append((sec.start, sec.end))

        if not scan_sections or not text_ranges:
            return {"error": "Required sections not found", "vtables": [], "total": 0, "named_count": 0}

        def in_text(addr):
            for start, end in text_ranges:
                if start <= addr < end:
                    return True
            return False

        def in_data(addr):
            for start, end in data_ranges:
                if start <= addr < end:
                    return True
            return False

        # Build type_info lookup: RVA (64-bit) or VA (32-bit) -> demangled name
        name_offset = 0x10 if is_64bit else 0x08
        ti_map = {}  # type_info_rva_or_va -> (demangled_name, type_info_addr)
        for s in bv.strings:
            val = s.value
            if not val or not val.startswith(".?A"):
                continue
            ti_addr = s.start - name_offset
            demangled = val
            try:
                _, qname = bn.demangle_ms(bv.arch, val)
                if qname:
                    demangled = "::".join(str(part) for part in qname)
            except Exception:
                pass
            if is_64bit:
                ti_map[ti_addr - module_base] = (demangled, ti_addr)
            else:
                ti_map[ti_addr] = (demangled, ti_addr)

        # Helper to read a pointer from section data at a given virtual address
        def read_ptr_at(addr):
            for _, sec_start, sec_data in scan_sections:
                sec_end = sec_start + len(sec_data)
                if sec_start <= addr < sec_end:
                    off = addr - sec_start
                    if off + ptr_size <= len(sec_data):
                        return struct.unpack_from(ptr_fmt, sec_data, off)[0]
            return None

        # Try to resolve COL at a given address and return class name info
        def try_resolve_col(col_ptr):
            if not in_data(col_ptr):
                return None
            if is_64bit:
                # x64 COL: sig(4)=1, offset(4), cdOffset(4), typeDescRVA(4),
                #          classHierRVA(4), selfRVA(4) = 24 bytes
                col_bytes_list = []
                for _, sec_start, sec_data in scan_sections:
                    sec_end = sec_start + len(sec_data)
                    if sec_start <= col_ptr < sec_end:
                        off = col_ptr - sec_start
                        if off + 24 <= len(sec_data):
                            col_bytes_list.append(sec_data[off:off + 24])
                        break
                if not col_bytes_list:
                    return None
                col_data = col_bytes_list[0]
                sig = struct.unpack_from("<I", col_data, 0)[0]
                if sig != 1:
                    return None
                self_rva = struct.unpack_from("<I", col_data, 20)[0]
                expected_self_rva = (col_ptr - module_base) & 0xFFFFFFFF
                if self_rva != expected_self_rva:
                    return None
                type_desc_rva = struct.unpack_from("<I", col_data, 12)[0]
                info = ti_map.get(type_desc_rva)
                if info:
                    return {"class_name": info[0], "type_info_addr": hex(info[1])}
            else:
                # x86 COL: sig(4)=0, offset(4), cdOffset(4), typeDescPtr(4) = 16 bytes
                col_bytes_list = []
                for _, sec_start, sec_data in scan_sections:
                    sec_end = sec_start + len(sec_data)
                    if sec_start <= col_ptr < sec_end:
                        off = col_ptr - sec_start
                        if off + 16 <= len(sec_data):
                            col_bytes_list.append(sec_data[off:off + 16])
                        break
                if not col_bytes_list:
                    return None
                col_data = col_bytes_list[0]
                sig = struct.unpack_from("<I", col_data, 0)[0]
                if sig != 0:
                    return None
                type_desc_ptr = struct.unpack_from("<I", col_data, 12)[0]
                info = ti_map.get(type_desc_ptr)
                if info:
                    return {"class_name": info[0], "type_info_addr": hex(info[1])}
            return None

        # Scan each data section for contiguous runs of .text pointers
        vtables = []
        seen_addrs = set()

        for sec_name, sec_start, sec_data in scan_sections:
            sec_len = len(sec_data)
            i = 0
            while i + ptr_size <= sec_len:
                val = struct.unpack_from(ptr_fmt, sec_data, i)[0]
                if not in_text(val):
                    i += ptr_size
                    continue

                # Found a .text pointer; count the run length
                run_start = i
                run_count = 0
                j = i
                while j + ptr_size <= sec_len:
                    v = struct.unpack_from(ptr_fmt, sec_data, j)[0]
                    if not in_text(v):
                        break
                    run_count += 1
                    j += ptr_size

                if run_count >= min_methods:
                    vtable_addr = sec_start + run_start
                    if vtable_addr not in seen_addrs:
                        seen_addrs.add(vtable_addr)

                        # Collect first few method addresses
                        first_methods = []
                        for k in range(min(run_count, 5)):
                            off = run_start + k * ptr_size
                            va = struct.unpack_from(ptr_fmt, sec_data, off)[0]
                            first_methods.append(hex(va))

                        # Try to resolve class name via COL at vtable[-1]
                        class_name = None
                        type_info_addr = None
                        if run_start >= ptr_size:
                            col_ptr = struct.unpack_from(
                                ptr_fmt, sec_data, run_start - ptr_size
                            )[0]
                            col_info = try_resolve_col(col_ptr)
                            if col_info:
                                class_name = col_info["class_name"]
                                type_info_addr = col_info["type_info_addr"]

                        vtables.append({
                            "vtable_addr": hex(vtable_addr),
                            "num_methods": run_count,
                            "class_name": class_name,
                            "type_info_addr": type_info_addr,
                            "section": sec_name,
                            "first_methods": first_methods,
                        })

                # Advance past this run
                i = j

        # Sort: named first (alphabetically), then unnamed (by address)
        vtables.sort(key=lambda v: (v["class_name"] is None, v["class_name"] or "", v["vtable_addr"]))

        total = len(vtables)
        named_count = sum(1 for v in vtables if v["class_name"] is not None)
        paginated = vtables[offset:offset + limit]

        return {
            "vtables": paginated,
            "total": total,
            "named_count": named_count,
            "offset": offset,
            "limit": limit,
        }

    def batch_label_data(self, labels: list[dict]) -> dict:
        """Name data variables at specified addresses.

        Args:
            labels: List of dicts with 'address' (hex string or int), 'name',
                    and optional 'size' (int, default 1).

        Returns:
            dict with 'success_count', 'failure_count', 'total', 'results'.
        """
        bv = self._current_view
        if not bv:
            return {
                "error": "No binary loaded",
                "success_count": 0,
                "failure_count": 0,
                "results": [],
            }

        results = []
        success_count = 0
        failure_count = 0

        for item in labels:
            addr_raw = item.get("address", "")
            name = item.get("name", "")
            size = item.get("size", 1)
            result_item = {"address": addr_raw, "name": name}

            try:
                if isinstance(addr_raw, int):
                    addr = addr_raw
                elif isinstance(addr_raw, str):
                    addr = int(addr_raw.strip(), 16)
                else:
                    result_item["status"] = "error"
                    result_item["error"] = f"Invalid address type: {type(addr_raw)}"
                    failure_count += 1
                    results.append(result_item)
                    continue

                # Define a data variable if none exists
                existing = bv.get_data_var_at(addr)
                if existing is None:
                    bv.define_user_data_var(addr, bn.Type.array(bn.Type.int(8, False), size))

                # Set the symbol name
                sym = bn.Symbol(bn.SymbolType.DataSymbol, addr, name)
                bv.define_user_symbol(sym)

                result_item["status"] = "ok"
                success_count += 1

            except Exception as e:
                result_item["status"] = "error"
                result_item["error"] = str(e)
                failure_count += 1

            results.append(result_item)

        return {
            "success_count": success_count,
            "failure_count": failure_count,
            "total": len(labels),
            "results": results,
        }

    def batch_create_functions(self, entries: list[dict]) -> dict:
        """Create function entries at addresses that lack them.

        Args:
            entries: List of dicts with 'address' (hex string or int) and
                     optional 'name' (string).

        Returns:
            dict with 'success_count', 'failure_count', 'total', 'results'.
        """
        bv = self._current_view
        if not bv:
            return {
                "error": "No binary loaded",
                "success_count": 0,
                "failure_count": 0,
                "results": [],
            }

        results = []
        success_count = 0
        failure_count = 0

        for item in entries:
            addr_raw = item.get("address", "")
            name = item.get("name", "")
            result_item = {"address": addr_raw, "name": name}

            try:
                if isinstance(addr_raw, int):
                    addr = addr_raw
                elif isinstance(addr_raw, str):
                    addr = int(addr_raw.strip(), 16)
                else:
                    result_item["status"] = "error"
                    result_item["error"] = f"Invalid address type: {type(addr_raw)}"
                    failure_count += 1
                    results.append(result_item)
                    continue

                # Check if function already exists
                existing = bv.get_function_at(addr)
                if existing:
                    if name:
                        old_name = existing.name
                        existing.name = name
                        result_item["status"] = "ok"
                        result_item["note"] = f"Already existed as {old_name}, renamed"
                    else:
                        result_item["status"] = "ok"
                        result_item["note"] = f"Already exists as {existing.name}"
                    success_count += 1
                else:
                    # Create function
                    ok = bv.create_user_function(addr)
                    if ok is not None:
                        if name:
                            func = bv.get_function_at(addr)
                            if func:
                                func.name = name
                        result_item["status"] = "ok"
                        result_item["note"] = "created"
                        success_count += 1
                    else:
                        result_item["status"] = "error"
                        result_item["error"] = "create_user_function returned None"
                        failure_count += 1

            except Exception as e:
                result_item["status"] = "error"
                result_item["error"] = str(e)
                failure_count += 1

            results.append(result_item)

        return {
            "success_count": success_count,
            "failure_count": failure_count,
            "total": len(entries),
            "results": results,
        }

    def scan_lea_operands(
        self, category: str = "", offset: int = 0, limit: int = 100
    ) -> dict:
        """Scan .text for instructions referencing known string addresses.

        Bypasses broken xref engines by iterating target string addresses
        and using bv.get_code_refs() or bv.get_data_refs() to find code
        that references them. Falls back to checking all function LLIL
        operands if get_code_refs returns nothing.

        Categories: lua_api, update_field, error_enum, rtti, source_path

        Returns dict with 'entries' list, 'total' count, and category breakdown.
        """
        bv = self._current_view
        if not bv:
            return {"error": "No binary loaded", "entries": [], "total": 0}

        # Phase 1: Build target set from binary strings
        targets = {}
        for s in bv.strings:
            val = s.value
            if not val:
                continue
            if val.startswith("Usage: ") or val.startswith("Usage:"):
                targets[s.start] = ("lua_api", val)
            elif "Data::" in val and val.startswith("CG"):
                targets[s.start] = ("update_field", val)
            elif val.startswith("ERROR_"):
                targets[s.start] = ("error_enum", val)
            elif val.startswith(".?AV") or val.startswith(".?AU"):
                targets[s.start] = ("rtti", val)
            elif "buildserver" in val.lower() and (
                val.lower().endswith(".cpp")
                or val.lower().endswith(".h")
                or val.lower().endswith(".cc")
            ):
                targets[s.start] = ("source_path", val)

        # Apply category filter
        if category:
            targets = {
                a: (c, v) for a, (c, v) in targets.items() if c == category
            }

        # Phase 2: Find code references to each target
        results = []
        cat_counts = {}

        for target_addr, (cat, val) in targets.items():
            refs = list(bv.get_code_refs(target_addr))
            if not refs:
                # Try data refs as fallback
                refs = list(bv.get_data_refs(target_addr))

            for ref in refs:
                func = None
                ref_addr = ref.address if hasattr(ref, "address") else ref
                funcs = bv.get_functions_containing(ref_addr)
                if funcs:
                    func = funcs[0]

                results.append(
                    {
                        "target_addr": hex(target_addr),
                        "ref_addr": hex(ref_addr),
                        "func_addr": hex(func.start) if func else None,
                        "func_name": func.name if func else None,
                        "category": cat,
                        "value": val[:200],  # Truncate long strings
                    }
                )
                cat_counts[cat] = cat_counts.get(cat, 0) + 1

        total = len(results)
        paginated = results[offset : offset + limit]

        return {
            "entries": paginated,
            "total": total,
            "offset": offset,
            "limit": limit,
            "target_count": len(targets),
            "category_breakdown": cat_counts,
        }

    def scan_lea_operands_raw(
        self, category: str = "", offset: int = 0, limit: int = 100
    ) -> dict:
        """Scan .text raw bytes for RIP-relative LEA/MOV instructions referencing
        known string addresses. Does NOT rely on BN's xref engine.

        This is the Arxan-resistant variant of scan_lea_operands. It reads raw
        .text bytes and decodes x86-64 RIP-relative addressing manually.

        On x86-64, LEA reg, [RIP+disp32] encodes as:
          REX prefix (optional 0x48/0x4C) + 0x8D + ModR/M(xx 000 101) + disp32
        MOV reg, [RIP+disp32] encodes similarly with opcode 0x8B.

        Categories: lua_api, update_field, error_enum, rtti, source_path
        """
        import struct as _struct

        bv = self._current_view
        if not bv:
            return {"error": "No binary loaded", "entries": [], "total": 0}

        # Phase 1: Build target address set from binary strings
        targets = {}
        for s in bv.strings:
            val = s.value
            if not val:
                continue
            if val.startswith("Usage: ") or val.startswith("Usage:"):
                targets[s.start] = ("lua_api", val)
            elif "Data::" in val and val.startswith("CG"):
                targets[s.start] = ("update_field", val)
            elif val.startswith("ERROR_"):
                targets[s.start] = ("error_enum", val)
            elif val.startswith(".?AV") or val.startswith(".?AU"):
                targets[s.start] = ("rtti", val)
            elif "buildserver" in val.lower() and (
                val.lower().endswith(".cpp")
                or val.lower().endswith(".h")
                or val.lower().endswith(".cc")
            ):
                targets[s.start] = ("source_path", val)

        if category:
            targets = {
                a: (c, v) for a, (c, v) in targets.items() if c == category
            }

        if not targets:
            return {
                "entries": [],
                "total": 0,
                "target_count": 0,
                "category_breakdown": {},
            }

        target_addrs = set(targets.keys())

        # Phase 2: Find .text section bounds
        text_start = None
        text_end = None
        for section in bv.sections.values():
            if section.name == ".text":
                text_start = section.start
                text_end = section.end
                break

        if text_start is None:
            return {"error": ".text section not found", "entries": [], "total": 0}

        text_size = text_end - text_start

        # Phase 3: Read .text raw bytes
        raw = bv.read(text_start, text_size)
        if not raw or len(raw) < text_size:
            return {
                "error": f"Failed to read .text ({text_size} bytes)",
                "entries": [],
                "total": 0,
            }

        # Phase 4: Scan for RIP-relative LEA and MOV instructions
        # Pattern: [REX] opcode ModR/M disp32
        # REX.W prefixes: 0x48, 0x4C (with REX.R)
        # LEA opcode: 0x8D, MOV opcode: 0x8B
        # ModR/M for RIP-relative: mode=00, R/M=101 -> (reg << 3) | 0x05
        # Valid ModR/M bytes: 0x05,0x0D,0x15,0x1D,0x25,0x2D,0x35,0x3D
        rip_modrm_bytes = {0x05, 0x0D, 0x15, 0x1D, 0x25, 0x2D, 0x35, 0x3D}
        results = []
        cat_counts = {}
        seen = set()

        i = 0
        while i < text_size - 7:
            b0 = raw[i]

            # Check for REX.W prefix (0x48 or 0x4C)
            rex = 0
            if b0 == 0x48 or b0 == 0x4C:
                rex = b0
                opcode = raw[i + 1] if i + 1 < text_size else 0
                modrm_off = i + 2
                instr_len = 7  # REX + opcode + ModR/M + disp32
            elif b0 == 0x8D or b0 == 0x8B:
                opcode = b0
                modrm_off = i + 1
                instr_len = 6  # opcode + ModR/M + disp32
            else:
                i += 1
                continue

            if opcode not in (0x8D, 0x8B):
                i += 1
                continue

            if modrm_off >= text_size:
                i += 1
                continue

            modrm = raw[modrm_off]
            if modrm not in rip_modrm_bytes:
                i += 1
                continue

            disp_off = modrm_off + 1
            if disp_off + 4 > text_size:
                i += 1
                continue

            disp32 = _struct.unpack_from("<i", raw, disp_off)[0]
            instr_addr = text_start + i
            # RIP-relative: target = instruction_end + displacement
            instr_end = text_start + i + instr_len
            target = instr_end + disp32

            if target in target_addrs:
                key = (instr_addr, target)
                if key not in seen:
                    seen.add(key)
                    cat, val = targets[target]

                    func = None
                    funcs = bv.get_functions_containing(instr_addr)
                    if funcs:
                        func = funcs[0]

                    results.append(
                        {
                            "instr_addr": hex(instr_addr),
                            "target_addr": hex(target),
                            "func_addr": hex(func.start) if func else None,
                            "func_name": func.name if func else None,
                            "category": cat,
                            "value": val[:200],
                        }
                    )
                    cat_counts[cat] = cat_counts.get(cat, 0) + 1

            i += 1

        total = len(results)
        paginated = results[offset : offset + limit]

        return {
            "entries": paginated,
            "total": total,
            "offset": offset,
            "limit": limit,
            "target_count": len(targets),
            "category_breakdown": cat_counts,
        }

    def _compute_mnemonic_hash(self, func):
        """Compute mnemonic-based hash for same-architecture matching."""
        import hashlib
        import struct

        hasher = hashlib.sha256()
        size = func.total_bytes
        hasher.update(struct.pack("<Q", size))

        mnemonic_count = 0
        for block in func.basic_blocks:
            for tokens, length in block:
                mnemonic = tokens[0].text.strip()
                if mnemonic:
                    hasher.update(mnemonic.encode("utf-8"))
                    mnemonic_count += 1

        if mnemonic_count == 0:
            return None

        hasher.update(struct.pack("<I", mnemonic_count))
        return hasher.hexdigest()

    def _compute_semantic_hash(self, func):
        """Compute semantic hash for cross-architecture matching.

        Hashes architecture-independent properties: instruction count,
        basic block count, callee count, integer constants > 0xFF,
        and referenced string constants.
        """
        import hashlib
        import struct

        bv = self._current_view
        hasher = hashlib.sha256()

        # Instruction count (not byte size)
        instr_count = 0
        for block in func.basic_blocks:
            for tokens, length in block:
                instr_count += 1

        if instr_count == 0:
            return None

        hasher.update(struct.pack("<I", instr_count))

        # Basic block count
        hasher.update(struct.pack("<I", len(func.basic_blocks)))

        # Callee count
        callees = set()
        for ref in func.call_sites:
            for callee in bv.get_functions_at(ref.address):
                callees.add(callee.start)
        # Also check MLIL calls
        try:
            for block in func.mlil.basic_blocks:
                for instr in block:
                    if hasattr(instr, 'dest') and hasattr(instr, 'operation'):
                        op_name = str(instr.operation)
                        if 'CALL' in op_name:
                            if hasattr(instr.dest, 'constant'):
                                callees.add(instr.dest.constant)
        except Exception:
            pass

        hasher.update(struct.pack("<I", len(callees)))

        # Integer constants > 0xFF from MLIL
        constants = set()
        try:
            for block in func.mlil.basic_blocks:
                for instr in block:
                    for operand in instr.prefix_operands:
                        if isinstance(operand, int) and operand > 0xFF:
                            constants.add(operand & 0xFFFFFFFFFFFFFFFF)
        except Exception:
            pass

        for c in sorted(constants):
            hasher.update(struct.pack("<Q", c))
        hasher.update(struct.pack("<I", len(constants)))

        # String references
        string_refs = []
        for block in func.basic_blocks:
            for ref in block.outgoing_edges:
                pass  # edges don't help here
        # Use data refs from the function's address range
        for ref_addr in range(func.start, func.start + func.total_bytes):
            pass  # Too slow, use IL instead

        # Use LLIL to find string references
        try:
            for block in func.llil.basic_blocks:
                for instr in block:
                    for operand in instr.prefix_operands:
                        if isinstance(operand, int):
                            s = bv.get_string_at(operand)
                            if s and s.length > 2:
                                string_refs.append(s.value)
        except Exception:
            pass

        for s in sorted(string_refs):
            hasher.update(s.encode("utf-8", errors="replace"))
        hasher.update(struct.pack("<I", len(string_refs)))

        return hasher.hexdigest()

    def propagate_symbols_export(
        self, offset: int = 0, limit: int = 5000, mode: str = "mnemonic"
    ) -> dict:
        """Export function hashes for cross-version symbol propagation.

        Two modes:
          mnemonic - SHA-256 of size + mnemonics + count (same-arch)
          semantic - SHA-256 of arch-independent properties (cross-arch)

        Hash format matches Ghidra's propagate_symbols.py for interoperability.
        """
        bv = self._current_view
        if not bv:
            return {"error": "No binary loaded", "entries": [], "total": 0}

        all_entries = []
        for func in bv.functions:
            name = func.name
            if not name or name.startswith("sub_"):
                continue

            if mode == "semantic":
                func_hash = self._compute_semantic_hash(func)
            else:
                func_hash = self._compute_mnemonic_hash(func)

            if func_hash is None:
                continue

            addr_width = 16 if bv.arch.name == "x86_64" else 8
            all_entries.append({
                "hash": func_hash,
                "address": f"{func.start:0{addr_width}X}",
                "name": name,
                "size": func.total_bytes,
                "mode": mode,
            })

        total = len(all_entries)
        paginated = all_entries[offset:offset + limit]

        return {
            "entries": paginated,
            "total": total,
            "offset": offset,
            "limit": limit,
            "mode": mode,
        }

    def propagate_symbols_import(self, source_hashes: list) -> dict:
        """Import function hashes and rename matching unnamed functions.

        source_hashes: list of dicts with 'hash', 'address', 'name', 'size',
        and optional 'mode' (defaults to 'mnemonic').

        Computes hashes for all unnamed functions in the current binary and
        matches against the provided source hashes. Renames matched functions.
        """
        bv = self._current_view
        if not bv:
            return {"error": "No binary loaded"}

        # Build lookup from source hashes, grouped by mode
        hash_lookup = {}  # hash -> (addr, name, size, mode)
        modes = set()
        for entry in source_hashes:
            h = entry["hash"]
            mode = entry.get("mode", "mnemonic")
            hash_lookup[h] = (
                entry["address"], entry["name"], int(entry["size"]), mode
            )
            modes.add(mode)

        matched = 0
        collisions = 0
        total = 0

        for func in bv.functions:
            name = func.name
            if not name.startswith("sub_"):
                continue

            total += 1

            for mode in modes:
                if mode == "semantic":
                    func_hash = self._compute_semantic_hash(func)
                else:
                    func_hash = self._compute_mnemonic_hash(func)

                if func_hash is None:
                    continue

                if func_hash in hash_lookup:
                    source_addr, source_name, source_size, source_mode = \
                        hash_lookup[func_hash]

                    if source_mode != mode:
                        continue

                    # Strict size check for mnemonic mode only
                    if mode == "mnemonic" and func.total_bytes != source_size:
                        collisions += 1
                        continue

                    func.name = source_name
                    func.comment = (
                        f"Matched from source build at {source_addr} "
                        f"({mode} mode)"
                    )
                    matched += 1
                    break

        return {
            "matched": matched,
            "collisions": collisions,
            "total_functions": total,
            "source_hashes_available": len(hash_lookup),
            "modes": list(modes),
        }
