"""Subprocess transport implementation using Claude Code CLI."""

import json
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from subprocess import PIPE
from typing import Any

import anyio
from anyio.abc import Process
from anyio.streams.text import TextReceiveStream

from ..._errors import CLIConnectionError, CLINotFoundError, ProcessError
from ..._errors import CLIJSONDecodeError as SDKJSONDecodeError
from ...types import ClaudeCodeOptions
from . import Transport


class SubprocessCLITransport(Transport):
    """Subprocess transport using Claude Code CLI."""

    def __init__(
        self,
        prompt: str,
        options: ClaudeCodeOptions,
        cli_path: str | Path | None = None,
    ):
        self._prompt = prompt
        self._options = options
        self._cli_path = str(cli_path) if cli_path else self._find_cli()
        self._cwd = str(options.cwd) if options.cwd else None
        self._process: Process | None = None
        self._stdout_stream: TextReceiveStream | None = None
        self._stderr_stream: TextReceiveStream | None = None

    def _find_cli(self) -> str:
        """Find Claude Code CLI binary."""
        if cli := shutil.which("claude"):
            return cli

        locations = [
            Path.home() / ".npm-global/bin/claude",
            Path("/usr/local/bin/claude"),
            Path.home() / ".local/bin/claude",
            Path.home() / "node_modules/.bin/claude",
            Path.home() / ".yarn/bin/claude",
        ]

        for path in locations:
            if path.exists() and path.is_file():
                return str(path)

        node_installed = shutil.which("node") is not None

        if not node_installed:
            error_msg = "Claude Code requires Node.js, which is not installed.\n\n"
            error_msg += "Install Node.js from: https://nodejs.org/\n"
            error_msg += "\nAfter installing Node.js, install Claude Code:\n"
            error_msg += "  npm install -g @anthropic-ai/claude-code"
            raise CLINotFoundError(error_msg)

        raise CLINotFoundError(
            "Claude Code not found. Install with:\n"
            "  npm install -g @anthropic-ai/claude-code\n"
            "\nIf already installed locally, try:\n"
            '  export PATH="$HOME/node_modules/.bin:$PATH"\n'
            "\nOr specify the path when creating transport:\n"
            "  SubprocessCLITransport(..., cli_path='/path/to/claude')"
        )

    def _build_command(self) -> list[str]:
        """Build CLI command with arguments."""
        cmd = [self._cli_path, "--output-format", "stream-json", "--verbose"]

        if self._options.system_prompt:
            cmd.extend(["--system-prompt", self._options.system_prompt])

        if self._options.append_system_prompt:
            cmd.extend(["--append-system-prompt", self._options.append_system_prompt])

        if self._options.allowed_tools:
            cmd.extend(["--allowedTools", ",".join(self._options.allowed_tools)])

        if self._options.max_turns:
            cmd.extend(["--max-turns", str(self._options.max_turns)])

        if self._options.disallowed_tools:
            cmd.extend(["--disallowedTools", ",".join(self._options.disallowed_tools)])

        if self._options.model:
            cmd.extend(["--model", self._options.model])

        if self._options.permission_prompt_tool_name:
            cmd.extend(
                ["--permission-prompt-tool", self._options.permission_prompt_tool_name]
            )

        if self._options.permission_mode:
            cmd.extend(["--permission-mode", self._options.permission_mode])

        if self._options.continue_conversation:
            cmd.append("--continue")

        if self._options.resume:
            cmd.extend(["--resume", self._options.resume])

        if self._options.mcp_servers:
            cmd.extend(
                ["--mcp-config", json.dumps({"mcpServers": self._options.mcp_servers})]
            )

        cmd.extend(["--print", self._prompt])
        return cmd

    async def connect(self) -> None:
        """Start subprocess."""
        if self._process:
            return

        cmd = self._build_command()
        try:
            self._process = await anyio.open_process(
                cmd,
                stdin=None,
                stdout=PIPE,
                stderr=PIPE,
                cwd=self._cwd,
                env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "sdk-py"},
            )

            if self._process.stdout:
                self._stdout_stream = TextReceiveStream(self._process.stdout)
            if self._process.stderr:
                self._stderr_stream = TextReceiveStream(self._process.stderr)

        except FileNotFoundError as e:
            raise CLINotFoundError(f"Claude Code not found at: {self._cli_path}") from e
        except Exception as e:
            raise CLIConnectionError(f"Failed to start Claude Code: {e}") from e

    async def disconnect(self) -> None:
        """Terminate subprocess."""
        if not self._process:
            return

        if self._process.returncode is None:
            try:
                self._process.terminate()
                with anyio.fail_after(5.0):
                    await self._process.wait()
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
            except ProcessLookupError:
                pass

        self._process = None
        self._stdout_stream = None
        self._stderr_stream = None

    async def send_request(self, messages: list[Any], options: dict[str, Any]) -> None:
        """Not used for CLI transport - args passed via command line."""

    async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
        """Receive messages from CLI."""
        if not self._process or not self._stdout_stream:
            raise CLIConnectionError("Not connected")

        stderr_lines = []

        async def read_stderr() -> None:
            """Read stderr in background."""
            if self._stderr_stream:
                try:
                    async for line in self._stderr_stream:
                        stderr_lines.append(line.strip())
                except anyio.ClosedResourceError:
                    pass

        async with anyio.create_task_group() as tg:
            tg.start_soon(read_stderr)

            try:
                json_buffer = ""
                brace_count = 0
                bracket_count = 0
                in_string = False
                escape_next = False
                MAX_BUFFER_SIZE = 50 * 1024 * 1024  # 50MB limit
                
                debug_json = os.environ.get("CLAUDE_CODE_DEBUG_JSON", "").lower() == "true"
                
                async for line in self._stdout_stream:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    
                    if debug_json:
                        print(f"[DEBUG] Line: {line_str[:100]}...")
                        print(f"[DEBUG] Buffer size: {len(json_buffer)}, Braces: {brace_count}, Brackets: {bracket_count}, In string: {in_string}")

                    # Check buffer size before adding
                    if len(json_buffer) + len(line_str) > MAX_BUFFER_SIZE:
                        # Buffer too large, likely a streaming issue
                        raise SDKJSONDecodeError(
                            f"JSON buffer exceeded {MAX_BUFFER_SIZE} bytes. Response too large.",
                            ValueError("Buffer overflow")
                        )
                    
                    # Add line to buffer
                    if json_buffer:
                        json_buffer += "\n" + line_str
                    else:
                        json_buffer = line_str
                    
                    # Count braces and brackets to detect complete JSON
                    # Only process the new line, not the entire buffer
                    i = 0
                    while i < len(line_str):
                        char = line_str[i]
                        
                        if in_string:
                            if escape_next:
                                escape_next = False
                            elif char == '\\':
                                escape_next = True
                            elif char == '"':
                                in_string = False
                        else:
                            if char == '"':
                                in_string = True
                            elif char == '{':
                                brace_count += 1
                            elif char == '}':
                                brace_count -= 1
                            elif char == '[':
                                bracket_count += 1
                            elif char == ']':
                                bracket_count -= 1
                        
                        i += 1
                    
                    # Check if we have a complete JSON object
                    if json_buffer and brace_count == 0 and bracket_count == 0 and not in_string:
                        try:
                            data = json.loads(json_buffer)
                            try:
                                yield data
                            except GeneratorExit:
                                # Handle generator cleanup gracefully
                                return
                            # Reset buffer and counters
                            json_buffer = ""
                            brace_count = 0
                            bracket_count = 0
                            in_string = False
                            escape_next = False
                        except json.JSONDecodeError as e:
                            # If it starts with JSON but fails to parse, it might be incomplete
                            if json_buffer.startswith("{") or json_buffer.startswith("["):
                                # Only continue if we haven't exceeded reasonable size
                                if len(json_buffer) < MAX_BUFFER_SIZE:
                                    continue
                                else:
                                    # Too large and still invalid
                                    raise SDKJSONDecodeError(json_buffer[:1000] + "...", e) from e
                            else:
                                # Not JSON, reset buffer and counters
                                json_buffer = ""
                                brace_count = 0
                                bracket_count = 0
                                in_string = False
                                escape_next = False
                                continue

            except anyio.ClosedResourceError:
                pass

        await self._process.wait()
        if self._process.returncode is not None and self._process.returncode != 0:
            stderr_output = "\n".join(stderr_lines)
            if stderr_output and "error" in stderr_output.lower():
                raise ProcessError(
                    "CLI process failed",
                    exit_code=self._process.returncode,
                    stderr=stderr_output,
                )

    def is_connected(self) -> bool:
        """Check if subprocess is running."""
        return self._process is not None and self._process.returncode is None
