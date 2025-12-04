"""
Minimal ToolRegistry class.

Generates five files in the cache directory:
 - strategic_tools.txt  (a simplified text format for strategic planning)
 - tools.txt            (a detailed text format for tool definitions)
 - gemini_tools.py      (a Python module with Gemini-compatible tool schemas)
 - ui_tools.json        (a JSON format for the UI plan editor)
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

    def generate_strategic(self, force: bool = False) -> dict:
        generated_files = {}
        for mode in ['vision', 'terminal']:
            out = os.path.join(self.get_cache_dir(), mode, 'strategic_tools.txt')
            generated_files[mode] = out
            if not force and os.path.exists(out):
                continue

            lines = []
            try:
                meta = self._get_filtered_metadata(mode)
                for meta_name, info in meta.items():
                    tool = info.get('tool') or {}
                    name = tool.get('name') or meta_name
                    args = []
                    for arg_name, arg_schema in (tool.get('arguments') or {}).items():
                        if isinstance(arg_schema, dict) and arg_schema.get('strategic') is False:
                            continue
                        args.append(arg_name)
                    args_str = ', '.join(args)
                    signature = f"{name}({args_str})"
                    strategic_text = tool.get('strategic') or tool.get('description') or ''
                    lines.append(f"- `{signature}`: {strategic_text}")
            except Exception:
                lines = []

            text = '\n'.join(lines)
            self._write_text(out, text)
        return generated_files

    def generate_tools(self, force: bool = False) -> dict:
        generated_files = {}
        for mode in ['vision', 'terminal']:
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

    def generate_gemini(self, force: bool = False) -> dict:
        generated_files = {}
        for mode in ['vision', 'terminal']:
            out = os.path.join(self.get_cache_dir(), mode, 'gemini_tools.py')
            generated_files[mode] = out
            if not force and os.path.exists(out):
                continue

            meta = self._get_filtered_metadata(mode)
            tools_list = [info.get('tool') for info in meta.values() if info.get('tool')]

            def pstr(s: str) -> str:
                if s is None:
                    return 'None'
                return repr(s)

            def map_type(t):
                if t is None:
                    return 'protos.Type.STRING'
                if isinstance(t, str):
                    tt = t.upper()
                    if tt in ('STRING', 'STR'):
                        return 'protos.Type.STRING'
                    if tt in ('INTEGER', 'INT', 'NUMBER'):
                        return 'protos.Type.INTEGER'
                    if tt in ('ARRAY', 'LIST'):
                        return 'protos.Type.ARRAY'
                    if tt in ('OBJECT', 'DICT', 'MAP'):
                        return 'protos.Type.OBJECT'
                try:
                    code = int(t)
                    if code == 1:
                        return 'protos.Type.STRING'
                    if code == 3:
                        return 'protos.Type.INTEGER'
                    if code == 5:
                        return 'protos.Type.ARRAY'
                except Exception:
                    pass
                return 'protos.Type.STRING'

            lines = [
                'from google.generativeai import protos', '',
                'def get_tools_schema():',
                '    """Return a list of protos.Tool instances equivalent to GoogleProvider._get_tools_schema()."""',
                '    return [',
                '        protos.Tool(function_declarations=['
            ]

            for t in tools_list:
                name = t.get('name', '')
                desc = t.get('description', '')
                args = t.get('arguments', {})
                required_keys = [k for k, v in args.items() if isinstance(v, dict) and v.get('required')]

                lines.extend([
                    '            protos.FunctionDeclaration(',
                    f'                name={pstr(name)},',
                    f'                description={pstr(desc)},',
                    '                parameters=protos.Schema(',
                    '                    type=protos.Type.OBJECT,',
                    '                    properties={'
                ])

                if isinstance(args, dict):
                    for arg_name, arg_schema in args.items():
                        if isinstance(arg_schema, dict):
                            a_type = arg_schema.get('type')
                            a_desc = arg_schema.get('description', '')
                            a_enum = arg_schema.get('enum', [])
                            a_items = arg_schema.get('items')
                        else:
                            a_type, a_desc, a_enum, a_items = None, str(arg_schema), [], None

                        lines.extend([
                            f'                        {pstr(arg_name)}: protos.Schema(',
                            f'                            type={map_type(a_type)},',
                            f'                            description={pstr(a_desc)},'
                        ])
                        if a_enum:
                            lines.append(f'                            enum=[{", ".join(pstr(e) for e in a_enum)}],')
                        if a_items is not None:
                            lines.append('                            items=protos.Schema(')
                            if isinstance(a_items, dict):
                                lines.append(f'                                type={map_type(a_items.get("type"))},')
                                if a_items.get("enum"):
                                    lines.append(f'                                enum=[{", ".join(pstr(e) for e in a_items.get("enum", []))}],')
                                lines.append(f'                                description={pstr(a_items.get("description", ""))},')
                            else:
                                lines.append(f'                                type={map_type(a_items)},')
                            lines.append('                            ),')
                        lines.append('                        ),')

                lines.extend([
                    '                    },',
                    f'                    required=[{", ".join(pstr(r) for r in required_keys)}]' if required_keys else '                    required=[]',
                    '                )',
                    '            ),'
                ])

            lines.extend(['        ])', '    ]', ''])
            code = '\n'.join(lines)
            self._write_text(out, code)
        return generated_files

    def generate_ui_tools(self, force: bool = False) -> dict:
        generated_files = {}
        for mode in ['vision', 'terminal']:
            out = os.path.join(self.get_cache_dir(), mode, 'ui_tools.json')
            generated_files[mode] = out
            if not force and os.path.exists(out):
                continue

            ui_actions = []
            try:
                meta = self._get_filtered_metadata(mode)
                for _, info in meta.items():
                    tool_meta = info.get('tool', {})
                    if not tool_meta:
                        continue

                    arguments = {arg_name: arg_info.get('default')
                                 for arg_name, arg_info in tool_meta.get('arguments', {}).items()}

                    action = {
                        "name": tool_meta.get('name'),
                        "description": tool_meta.get('description'),
                        "arguments": arguments
                    }
                    ui_actions.append(action)
            except Exception:
                pass

            with open(out, 'w', encoding='utf-8') as f:
                json.dump(ui_actions, f, indent=4)
        return generated_files

    def generate_response_formats(self, force: bool = False) -> dict:
        """
        Generates JSON files with response format instructions for the LLM for all modes.
        """
        generated_files = {}
        for mode in ['vision', 'terminal', 'chat']:
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
                else:  # terminal
                    from src.computer.schemas.terminal import generate_response_formats_dict

                formats_dict = generate_response_formats_dict()
            except Exception as e:
                import logging
                logging.error(f"Failed to generate response formats for mode '{mode}': {e}", exc_info=True)
                formats_dict = {}

            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, 'w', encoding='utf-8') as f:
                json.dump(formats_dict, f, indent=2)
        return generated_files

    def generate_all(self, force: bool = False) -> dict:
        start = time.perf_counter()
        results = {
            'strategic': self.generate_strategic(force=force),
            'tools': self.generate_tools(force=force),
            'gemini': self.generate_gemini(force=force),
            'ui_tools': self.generate_ui_tools(force=force),
            'response_formats': self.generate_response_formats(force=force),
        }
        duration = time.perf_counter() - start
        logging.info(f"ToolRegistry.generate_all completed in {duration:.3f}s")
        return results

if __name__ == '__main__':
    tr = ToolRegistry()
    print(tr.generate_all(force=True))
