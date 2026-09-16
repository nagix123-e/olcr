"""Public CLI vocabulary; GUI adapters must explicitly opt in to execution."""
from dataclasses import asdict, dataclass

@dataclass(frozen=True)
class Command:
    command: str
    description: str
    arguments: str = ""
    requires_project: bool = False
    enabled: bool = True
    interaction_type: str = "structured"
    reason: str = ""

    def public(self):
        return asdict(self) | {"id": self.command[1:].replace(" ", "_"),
            "family": self.command.split()[0], "title": self.command[1:],
            "requires_conversation": False,
            "read_only":self.command.split()[-1] in {"show","status","help","models","setup"},
            "handler":"registered_api_adapter" if self.enabled else "cli_session"}

COMMANDS = [Command("/help", "List registered commands"), Command("/status", "Backend status"),
    Command("/models", "Configured model roles")]
for action in ("show", "on", "off"):
    COMMANDS.append(Command("/memory " + action, "Global conversation memory: " + action))
for action in ("show", "set"):
    COMMANDS.append(Command("/workspace " + action, "Project workspace: " + action,
        "path" if action == "set" else "", True))
for action in ("show", "set", "load", "reload", "clear"):
    COMMANDS.append(Command("/context " + action, "Project Core Context: " + action,
        "text" if action == "set" else "path" if action == "load" else "", True))
for action in ("show", "off", "manual", "auto", "status", "setup"):
    COMMANDS.append(Command("/web " + action, "Global Web search: " + action))
for action in ("status", "on", "off"):
    COMMANDS.append(Command("/external " + action, "Global External Tools network access: " + action))
COMMANDS.extend([
    Command("/weather", "Weather via Open-Meteo", "location"),
    Command("/currency", "Currency conversion via Frankfurter", "amount BASE QUOTE"),
    Command("/research", "Research papers via OpenAlex", "query"),
    Command("/wiki", "Wikipedia knowledge lookup", "query"),
])
for action in ("show", "brave", "tavily", "clear"):
    COMMANDS.append(Command("/web provider " + action, "Web provider: " + action))
COMMANDS.append(Command("/option show", "Show configured model roles"))
for family, actions in {"file": ("set", "show", "clear"), "image": ("load", "show", "clear"),
        "external": ("set", "show", "clear"), "web": ("open", "clear"),
        "import": ("external",), "option": ("set", "reset")}.items():
    for action in actions:
        COMMANDS.append(Command(f"/{family} {action}", f"CLI {family}: {action}",
            enabled=False, interaction_type="cli_only",
            reason="Requires the CLI session's grants, attachments or interactive model validation."))
for command in ("/quit", "/exit"):
    COMMANDS.append(Command(command, "Exit CLI", enabled=False, interaction_type="cli_only",
        reason="Close the desktop window using its window controls."))

def catalog():
    return [command.public() for command in COMMANDS]

def resolve(text):
    for command in sorted(COMMANDS, key=lambda c: -len(c.command)):
        if text == command.command or text.startswith(command.command + " "):
            argument = text[len(command.command):].strip()
            if argument and not command.arguments:
                raise ValueError("UNEXPECTED_ARGUMENT")
            return command, argument
    raise ValueError("UNKNOWN_COMMAND: use /help")
