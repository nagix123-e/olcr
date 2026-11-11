"""Public CLI vocabulary; GUI adapters must explicitly opt in to execution."""
from dataclasses import asdict, dataclass
import re

def workspace_command(text: str) -> str | None:
    from .git_workspace import git_intent
    git = git_intent(text)
    if git:
        import json
        arguments = {key:value for key,value in git.items() if key != 'action'}
        return '/git ' + git['action'] + (' ' + json.dumps(arguments, ensure_ascii=False) if arguments else '')
    value = re.sub(r"[。.!！]+$", "", text.strip()).strip()
    patterns = {
        "/settings": r"(?:設定(?:を)?(?:開いて|表示して)|権限設定(?:を)?(?:開いて|表示して)|open settings|show settings)",
        "/scrape manage": r"(?:スクレイピング(?:の)?管理(?:を)?(?:開いて|表示して)|スクレイプ管理|open scrape (?:manager|studio)|manage scraping)",
        "/routines": r"(?:ルーチン(?:の)?(?:一覧|管理)(?:を)?(?:表示して|開いて)?|検証済みルーチン(?:を)?(?:表示して|開いて)|show routines|manage routines)",
    }
    for command, pattern in patterns.items():
        if re.fullmatch(pattern, value, re.I):
            return command
    toggle = re.fullmatch(r"(スクレイピング|ローカルE2E|コーディング)(?:を)?(オン|オフ)(?:にして)?", value, re.I)
    if toggle:
        family = {"スクレイピング": "scrape", "ローカルe2e": "e2e", "コーディング": "coding"}[toggle[1].lower()]
        return f"/{family} {'on' if toggle[2] == 'オン' else 'off'}"
    return None

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
            "read_only":self.command == "/screen" or self.command.split()[-1] in {"show","status","help","models","setup"} or self.command in {'/git diff','/git log','/git connect','/git prs'},
            "handler":"registered_api_adapter" if self.enabled else "cli_session"}

COMMANDS = [Command("/help", "List registered commands"), Command("/status", "Backend status"),
    Command("/models", "Configured model roles"),
    Command("/screen", "Resolve a question from this window's DOM/Accessibility state without Vision", "question", True),
    Command("/computer open", "Open a grounded disclosure in this OLCR window and verify its state", "label", True),
    Command("/computer use on", "Enable chat-triggered native Computer Use"),
    Command("/computer use off", "Disable chat-triggered native Computer Use")]
for family, actions in {'timer': ('start', 'list', 'pause', 'resume', 'cancel'),
                        'calendar': ('list', 'add', 'change', 'delete', 'connect'),
                        'youtube': ('play', 'pause', 'resume', 'stop')}.items():
    for action in actions:
        COMMANDS.append(Command(f'/{family} {action}', f'{family}: {action}', 'duration, title or URL'))
COMMANDS.extend([
    Command('/connectors', 'Connect personal services and configure permissions'),
    Command('/service', 'Read or send email through a connected service, or review a merchant checkout', 'service.google.gmail list {}'),
    Command("/settings", "Configure permissions and credentials in this chat"),
    Command("/scrape manage", "Manage browser profiles, recipes and runs in this chat"),
    Command("/routines", "Review verified traces and routine candidates in this chat"),
    Command("/routines compile", "Compile 2–8 confirmed traces", "trace IDs"),
    Command("/routines approve", "Approve a candidate review without activating it", "candidate ID"),
    Command("/routines reject", "Reject a candidate review", "candidate ID"),
    Command("/routines match", "Compare a goal with fresh World State without execution", "goal"),
])
for action in ('status', 'diff', 'log', 'connect', 'stage', 'commit', 'commit_push', 'push', 'branch', 'switch', 'prs', 'pr', 'approve', 'reject'):
    COMMANDS.append(Command('/git ' + action, 'Project Git: ' + action,
                            'JSON with paths and message' if action in ('stage', 'commit', 'commit_push') else '[remote] [branch]' if action == 'push' else 'branch name' if action in ('branch', 'switch') else 'PR number or creation JSON' if action == 'pr' else 'operation ID' if action in ('approve', 'reject') else '', True))
for family in ("scrape", "e2e", "coding-orchestrator"):
    for action in ("on", "off"):
        COMMANDS.append(Command(f"/{family} {action}", (f"Coding orchestrator: {action}" if family == "coding-orchestrator" else f"{family} capability: {action}")))
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
    Command("/skill", "Use a Skill for the current project or request", "name [prompt]"),
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
    text = workspace_command(text) or text
    if text in {"/coding on", "/coding off"}:
        text = text.replace("/coding ", "/coding-orchestrator ", 1)
    for command in sorted(COMMANDS, key=lambda c: -len(c.command)):
        if text == command.command or text.startswith(command.command + " "):
            argument = text[len(command.command):].strip()
            if argument and not command.arguments:
                raise ValueError("UNEXPECTED_ARGUMENT")
            return command, argument
    raise ValueError("UNKNOWN_COMMAND: use /help")
