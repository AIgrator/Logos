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
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)



# Note: no static fallback text; generator writes strictly from src.computer.local_desktop_computer.TOOLS_METADATA

# Bump when the shape of the generated response-format files changes so that
# stale caches (which are otherwise only invalidated by manifest changes) are
# rebuilt. v2: added `strict_schema`. v3: added `strict_schema_flat` (browser).
SCHEMA_GEN_VERSION = 3


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
        Loads tool metadata from the specified mode file.
        """
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
        return filtered_meta

    def generate_tools(self, force: bool = False) -> dict:
        generated_files = {}
        for mode in ['vision', 'terminal', 'browser']:
            dest = os.path.join(self.get_cache_dir(), mode, 'tools.txt')
            generated_files[mode] = dest
            if not force and os.path.exists(dest):
                continue

            lines = []
            try:
                meta = self._get_filtered_metadata(mode)
                for meta_name, info in meta.items():
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
            except Exception:
                lines = []

            data = '\n'.join(lines).rstrip() + '\n'
            self._write_text(dest, data)
        return generated_files

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
                        meta = self._get_filtered_metadata(mode)
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
        }
        self._write_manifest_fingerprint()
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
