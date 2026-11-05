"""
Minimal ToolRegistry class.

Generates two files in the cache directory:
 - tools.txt            (a detailed text format for tool definitions)
 - response_formats.json (a JSON with response format instructions for the LLM)

This file intentionally keeps behaviour minimal and predictable per user request.
"""
from __future__ import annotations

import importlib
import json
import os
from typing import TYPE_CHECKING
import sys
import time
import logging

if TYPE_CHECKING:
    from src.settings_manager import SettingsManager

# Add the project root to the Python path
# This is necessary for the script to be runnable directly, and for the dynamic module loading to work.
if getattr(sys, 'frozen', False):
    project_root = sys._MEIPASS
else:
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)



# Note: no static fallback text; generator writes strictly from src.computer.local_desktop_computer.TOOLS_METADATA

# Bump when the shape of the generated response-format files changes so that
# stale caches (which are otherwise only invalidated by manifest changes) are
# rebuilt. v2: added `strict_schema`. v3: added `strict_schema_flat` (browser).
# v4: payload.commentary narration + update_plan. v5: payload.actions[] batch.
SCHEMA_GEN_VERSION = 5


class ToolRegistry:
    def __init__(self, settings_manager: SettingsManager):
        self._settings_manager = settings_manager
        self._metadata_cache = None

    def get_cache_dir(self) -> str:
        # Always get the cache directory from the central settings manager
        return self._settings_manager.get('APP_CACHE_DIR')

    def _write_text(self, path: str, text: str) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def _get_filtered_metadata(self, mode: str) -> dict:
        """
        Loads tool metadata for a mode, keeping only what its grants allow.

        ``src.computer.capabilities`` is the authority on membership; the mode
        module only supplies implementations. A tool that one side knows and
        the other does not is logged, so a forgotten registration cannot
        silently add a tool to the model's contract or drop one from it.
        """
        from src.computer import capabilities

        filtered_meta = {}
        try:
            mode_module_name = f'src.computer.modes.{mode}'
            mode_module = importlib.import_module(mode_module_name)

            if hasattr(mode_module, 'get_tools_metadata'):
                raw_metadata = mode_module.get_tools_metadata()
                for tool_name, tool_func in raw_metadata.items():
                    tool_info = getattr(tool_func, '__tool_meta__', None)
                    if tool_info:
                        filtered_meta[tool_name] = tool_info
                    else:
                        import logging
                        logging.warning(f"Tool '{tool_name}' in mode '{mode}' missing __tool_meta__ attribute.")
                        filtered_meta[tool_name] = {
                            "tool": {"name": tool_name, "description": tool_func.__doc__ or ""},
                            "flags": {"platform": "any", "mode": mode}
                        }
        except Exception as e:
            import logging
            logging.error(f"Error loading metadata for mode '{mode}': {e}", exc_info=True)
            return {}

        granted = set(capabilities.tools_for_mode(mode))
        declared = set(filtered_meta)
        if granted != declared:
            logging.warning(
                "Tool manifest disagreement for mode '%s': only in grants %s, "
                "only in the mode module %s (src/computer/capabilities.py is "
                "authoritative).",
                mode,
                sorted(granted - declared),
                sorted(declared - granted),
            )
        return {name: info for name, info in filtered_meta.items() if name in granted}

    def generate_capability_catalogue(self, force: bool = False) -> str:
        """Writes the capability → tool-name catalogue the grants are read from.

        One file for every capability keeps the cache honest about what a
        session may use, instead of four per-mode lists that can drift.
        """
        from src.computer import capabilities as caps

        dest = os.path.join(self.get_cache_dir(), "capabilities", "tools.json")
        if not force and os.path.exists(dest):
            return dest
        catalogue = {name: list(caps.TOOLS.get(name, ())) for name in caps.CAPABILITIES}
        catalogue["_modes"] = {mode: list(grants) for mode, grants in caps.MODE_GRANTS.items()}
        catalogue["_catalog"] = sorted(caps.CATALOG_TOOLS)
        self._write_text(dest, json.dumps(catalogue, indent=2) + "\n")
        return dest

    def generate_tools(self, force: bool = False) -> dict:
        from src.computer import capabilities as caps

        generated_files = {}
        for mode in ['vision', 'terminal', 'browser']:
            dest = os.path.join(self.get_cache_dir(), mode, 'tools.txt')
            generated_files[mode] = dest
            if not force and os.path.exists(dest):
                continue

            try:
                meta = self._declared_metadata(mode)
                data = self._tools_text(meta, caps.catalog_tools_for_mode(mode))
            except Exception:
                data = '\n'

            self._write_text(dest, data)
        return generated_files

    def _declared_metadata(self, mode: str) -> dict:
        """A mode's metadata without its on-demand tools.

        The per-mode files follow the same exposure split as a session view, so
        a pure ``base+terminal`` / ``base+browser`` grant set still produces the
        mode's block byte for byte.
        """
        from src.computer import capabilities as caps

        native = set(caps.native_tools_for_grants(caps.grants_for_mode(mode)))
        return {name: info for name, info in self._get_filtered_metadata(mode).items()
                if name in native}

    def _tools_text(self, meta: dict, catalog=()) -> str:
        """The ``{available_tools}`` block for one metadata set, in its order.

        Tools that are granted but not declared are named in a closing section:
        the model calls ``search_tools`` to load one, and it is declared from
        the next request on. Names only, because the whole point is to keep the
        declared set small.
        """
        lines = []
        for meta_name, info in (meta or {}).items():
            tool = info.get('tool') or {}
            name = tool.get('name') or meta_name
            desc = tool.get('description') or tool.get('strategic') or ''
            lines.append(f"- name: {name}")
            if desc:
                q = desc.replace('"', '\\"')
                lines.append(f"  description: \"{q}\"")

            args = tool.get('arguments') or {}
            if args:
                lines.append("  arguments:")
                for arg_name, arg_schema in args.items():
                    if isinstance(arg_schema, dict):
                        a_desc = arg_schema.get('description') or ''
                    else:
                        a_desc = str(arg_schema)
                    a_desc_q = a_desc.replace('"', '\\"')
                    lines.append(f"    {arg_name}: \"{a_desc_q}\"")
            lines.append("")
        text = '\n'.join(lines).rstrip() + '\n'
        names = [str(name).strip() for name in (catalog or ()) if str(name).strip()]
        if names:
            text += ("\nTools on demand (granted, not declared): call "
                     "search_tools(\"<tool name or need>\") to load one; it "
                     "becomes callable on the next step.\n")
            text += '\n'.join(f"- {name}" for name in names) + '\n'
        return text

    def generate_response_formats(self, force: bool = False) -> dict:
        """
        Generates JSON files with response format instructions for the LLM for all modes.
        """
        generated_files = {}
        for mode in ['vision', 'terminal', 'chat', 'browser']:
            out = os.path.join(self.get_cache_dir(), mode, 'response_formats.json')
            generated_files[mode] = out
            if not force and os.path.exists(out):
                continue

            formats_dict = {}
            try:
                if mode == 'vision':
                    from src.computer.schemas.vision import generate_response_formats_dict
                elif mode == 'chat':
                    from src.computer.schemas.chat import generate_response_formats_dict
                elif mode == 'browser':
                    from src.computer.schemas.browser import generate_response_formats_dict
                else:  # terminal
                    from src.computer.schemas.terminal import generate_response_formats_dict

                formats_dict = generate_response_formats_dict()

                # Attach a strict (grammar-constrained) variant of the tool-call
                # envelope, rebuilt from the manifests so the tool name and every
                # argument is fully typed and closed. Providers that support
                # constrained tool calling use this instead of `schema`.
                if mode in ('terminal', 'browser') and formats_dict:
                    try:
                        from src.computer.schemas.strict import (
                            build_strict_schema,
                            build_strict_schema_flat,
                        )
                        meta = self._declared_metadata(mode)
                        if meta:
                            for fname, fmt in formats_dict.items():
                                fmt['strict_schema'] = build_strict_schema(
                                    fname, meta, description=fmt.get('description'))
                                # Browser tools all accept **kwargs, so the
                                # single-branch fallback (which makes the model
                                # over-fill harmless extras) is safe there.
                                if mode == 'browser':
                                    fmt['strict_schema_flat'] = build_strict_schema_flat(
                                        fname, meta, description=fmt.get('description'))
                    except Exception as e:
                        logging.error(f"Failed to build strict schema for mode '{mode}': {e}", exc_info=True)
            except Exception as e:
                logging.error(f"Failed to generate response formats for mode '{mode}': {e}", exc_info=True)
                formats_dict = {}

            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, 'w', encoding='utf-8') as f:
                json.dump(formats_dict, f, indent=2)
        return generated_files

    def generate_session_profile(self, grants, handlers=None, force: bool = False,
                                 promote=()) -> dict:
        """Builds the tool block and strict schema for one session's grants.

        A session may hold several capabilities at once, so its tool list is no
        longer a mode constant. Each agent handler gets its own view (the tools
        it can execute for these grants, see ``capabilities.HANDLER_CAPABILITIES``)
        under ``cache/session/<handler>/``; the provider layer merges those files
        last, so the granted set wins over the per-mode definition of the same
        envelope name. A pure terminal or browser grant set produces exactly the
        per-mode block, so nothing the model sees changes for today's sessions.

        ``promote`` lists catalog tools the model loaded through ``search_tools``:
        they join the declared set (and the strict schema) from this request on,
        while the rest stay named in the prompt's on-demand section only.
        """
        from src.computer import capabilities

        grants = capabilities.normalize_grants(grants)
        if not ({capabilities.TERMINAL, capabilities.BROWSER} & set(grants)):
            # No agent capability in this session: blank the views so a previous
            # session's granted set cannot keep overriding the catalogue.
            self._clear_session_views(grants)
            return {}
        wanted = ([str(name).strip().lower() for name in handlers]
                  if handlers else list(capabilities.HANDLER_SCHEMAS))
        views = {}
        for handler in wanted:
            if handler not in capabilities.HANDLER_SCHEMAS:
                continue
            tools = capabilities.tools_for_handler(handler, grants, promote)
            if not tools:
                continue
            catalog = capabilities.catalog_tools_for_handler(handler, grants, promote)
            views[handler] = self._generate_session_view(
                handler, grants, tools, force=force, catalog=catalog)
        return views

    def _session_dir(self, handler: str) -> str:
        return os.path.join(self.get_cache_dir(), 'session', handler)

    def _clear_session_views(self, grants) -> None:
        """Empties the session files (a session without an agent capability)."""
        signature = {"grants": list(grants), "tools": [], "handler": "",
                     "gen": SCHEMA_GEN_VERSION}
        for handler in ("terminal", "browser"):
            dest = self._session_dir(handler)
            self._write_text(os.path.join(dest, 'tools.txt'), '')
            self._write_text(os.path.join(dest, 'response_formats.json'), '{}')
            self._write_text(os.path.join(dest, 'profile.json'),
                             json.dumps({**signature, "handler": handler}, indent=2) + '\n')

    def _generate_session_view(self, handler: str, grants, tools, force: bool = False,
                               catalog=()) -> dict:
        """One handler's tool block and response format for a grant set."""
        dest = self._session_dir(handler)
        tools_path = os.path.join(dest, 'tools.txt')
        formats_path = os.path.join(dest, 'response_formats.json')
        meta_path = os.path.join(dest, 'profile.json')
        signature = {
            "grants": list(grants),
            "tools": list(tools),
            "catalog": list(catalog),
            "handler": handler,
            "gen": SCHEMA_GEN_VERSION,
        }
        if (not force and os.path.exists(tools_path)
                and os.path.exists(formats_path) and os.path.exists(meta_path)):
            try:
                with open(meta_path, encoding='utf-8') as handle:
                    if json.load(handle) == signature:
                        return {"tools": tools_path, "response_formats": formats_path,
                                "changed": False}
            except (OSError, json.JSONDecodeError):
                pass

        meta = self._metadata_for_tools(tools, handler)
        self._write_text(tools_path, self._tools_text(meta, catalog))

        schema_name = None
        formats = {}
        try:
            from src.computer import capabilities
            schema_name = capabilities.HANDLER_SCHEMAS.get(handler)
        except Exception:
            schema_name = None
        if schema_name:
            try:
                module = importlib.import_module(f'src.computer.schemas.{handler}')
                entry = module.generate_response_formats_dict().get(schema_name)
            except Exception as error:
                logging.error(
                    "Could not build the %s response format for grants %s: %s",
                    handler, list(grants), error, exc_info=True)
                entry = None
            if entry:
                entry = dict(entry)
                if meta:
                    from src.computer.schemas.strict import (
                        build_strict_schema,
                        build_strict_schema_flat,
                    )
                    description = entry.get('description')
                    entry['strict_schema'] = build_strict_schema(
                        schema_name, meta, description=description)
                    # Browser tools all accept **kwargs, so the single-branch
                    # fallback is safe there (see BUILD_STRICT_SCHEMA_FLAT).
                    if handler == 'browser':
                        entry['strict_schema_flat'] = build_strict_schema_flat(
                            schema_name, meta, description=description)
                formats[schema_name] = entry
        self._write_text(formats_path, json.dumps(formats, indent=2))
        self._write_text(meta_path, json.dumps(signature, indent=2) + '\n')
        return {"tools": tools_path, "response_formats": formats_path,
                "changed": True}

    def _metadata_for_tools(self, tools, handler=None) -> dict:
        """Manifest entries for a tool-name list, in declaration order.

        The handler's own manifest is read first, so a grant set equal to a mode
        produces that mode's block byte for byte; tools of a second capability
        are appended after it.
        """
        from src.computer import capabilities

        wanted = set(tools or ())
        sources = []
        if handler in (capabilities.TERMINAL, capabilities.BROWSER):
            sources.append(handler)
        for capability in (capabilities.TERMINAL, capabilities.BROWSER):
            if capability not in sources:
                sources.append(capability)
        merged = {}
        for capability in sources:
            try:
                meta = self._get_filtered_metadata(capability)
            except Exception:
                continue
            for name, info in meta.items():
                if name in wanted and name not in merged:
                    merged[name] = info
        return merged

    def metadata_for_tools(self, tools, handler=None) -> dict:
        """Public manifest view for a tool-name list (``search_tools``)."""
        return self._metadata_for_tools(tools, handler)

    def definition_text(self, tools, handler=None) -> str:
        """The prompt block for just these tools, without the catalog section."""
        return self._tools_text(self._metadata_for_tools(tools, handler))

    def generate_all(self, force: bool = False) -> dict:
        start = time.perf_counter()
        # Manifest fingerprint: prompt/cache files must never diverge from the
        # manifests they are generated from. If tool membership changed since
        # the last generation, force regeneration regardless of `force`.
        if not force and self._manifests_changed():
            logging.info("Tool manifests changed since last generation; forcing cache regeneration.")
            force = True
        results = {
            'tools': self.generate_tools(force=force),
            'response_formats': self.generate_response_formats(force=force),
            'capabilities': self.generate_capability_catalogue(force=force),
        }
        self._write_manifest_fingerprint()
        # Startup has no open session: drop any per-session view left by the
        # previous run so a new session's grants are the only thing that shapes
        # the catalogue (each handler rebuilds its own view on the next turn).
        self._clear_session_views(())
        duration = time.perf_counter() - start
        logging.info(f"ToolRegistry.generate_all completed in {duration:.3f}s")
        return results

    def _manifest_fingerprint(self) -> dict:
        """Stable fingerprint of tool definitions per mode manifest.

        Hashes names AND descriptions AND arguments (not just membership):
        any prompt-visible change forces regeneration, so the model always
        sees the current contract (e.g. a new `press_enter` argument).
        """
        import hashlib
        from src.computer import capabilities

        fingerprint = {}
        for mode in ['vision', 'terminal', 'chat', 'browser']:
            try:
                meta = self._get_filtered_metadata(mode)
                canonical = json.dumps(meta, sort_keys=True, default=str)
                fingerprint[mode] = {
                    "gen": SCHEMA_GEN_VERSION,
                    "tools": sorted(meta.keys()),
                    "digest": hashlib.md5(canonical.encode('utf-8')).hexdigest(),
                }
            except Exception:
                fingerprint[mode] = {"tools": [], "digest": ""}
        fingerprint["capabilities"] = {
            "sets": {name: list(capabilities.TOOLS.get(name, ())) for name in capabilities.CAPABILITIES},
            "modes": {mode: list(grants) for mode, grants in capabilities.MODE_GRANTS.items()},
            "catalog": sorted(capabilities.CATALOG_TOOLS),
        }
        return fingerprint

    def _fingerprint_path(self) -> str:
        return os.path.join(self.get_cache_dir(), '.manifest_fingerprint.json')

    def _manifests_changed(self) -> bool:
        try:
            path = self._fingerprint_path()
            if not os.path.exists(path):
                return True
            with open(path, encoding='utf-8') as f:
                old = json.load(f)
            return old != self._manifest_fingerprint()
        except Exception:
            return True

    def _write_manifest_fingerprint(self) -> None:
        try:
            with open(self._fingerprint_path(), 'w', encoding='utf-8') as f:
                json.dump(self._manifest_fingerprint(), f, indent=2)
        except Exception as e:
            import logging
            logging.error(f"Failed to write manifest fingerprint: {e}", exc_info=True)

if __name__ == '__main__':
    sys.path.insert(0, os.path.join(project_root, 'src'))
    from settings_manager import SettingsManager
    tr = ToolRegistry(settings_manager=SettingsManager())
    print(tr.generate_all(force=True))
