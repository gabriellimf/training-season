"""A scripted console for the commands that read athlete lines."""

from dataclasses import dataclass, field


@dataclass
class ScriptedConsole:
    """Answers each prompt with the next line; EOF when the lines run out."""

    lines: list[str]
    prompts: list[str] = field(default_factory=list)

    def read_line(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)
