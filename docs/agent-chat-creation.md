# Create custom agents in chat

Ask chat to create a custom agent, or choose **+ → Create custom agent** and then
send your requirements or material. The composer shows creation mode. The mode
and pending questions belong to the conversation; switching conversations does
not move its draft. Pending drafts also survive a backend restart.

## Transfer existing material

Supplied prompts, knowledge files, agent JSON definitions and agent ZIPs use the
deterministic transfer route. This route does not call a generation model.
It preserves the original prompt text, knowledge-file bytes and supplied settings.
Unspecified optional settings retain the ordinary agent defaults.

For example:

```text
カスタムエージェントを作成
名前: 翻訳サポート
プロンプト:
あなたは翻訳者です。
入力を日本語に翻訳してください。
```

Everything following the prompt label is the prompt body; embedded labels and
trailing whitespace are preserved. Put metadata such as name before the prompt.
You can also attach a text file and specify that it is the prompt, attach knowledge
files, a knowledge-set ZIP, or a complete `.olcr-agent.zip`.

A knowledge-set ZIP may contain folders of supported text/PDF/image files. Safe
relative filenames and original bytes are retained; Finder metadata is ignored.
The existing knowledge limits, file type checks and native extraction/OCR apply.
A complete agent ZIP also copies its icon, all definition fields and linked agents.

If the name or the role of a supplied file is missing, chat asks for it. Unlabelled
supplied text is clarified as instructions, knowledge or generation requirements;
it is never silently rewritten. If the referenced earlier attachment is unavailable,
chat requests the original again instead of generating substitute material.

## Generate from requirements

Natural descriptions such as
`開発計画をユーザーに実装詳細について質問しながら立ててくれるエージェントを作りたい`
are generation requirements, including when their topic is software implementation.
With creation mode selected, a behavior description such as
`開発計画を質問しながら立ててくれる` also starts generation. Repeating a clear
creation request recovers text drafts misclassified by the former implementation
keyword exclusion. Original files are never reinterpreted through this recovery.
Clarification questions are retained alongside answers for the next model turn.

For example:

```text
名前: 英語メール練習
条件: 中級者向けに、仕事の英文メールの練習と添削をする。
指示と参考知識は生成してほしい。
```

The configured Brain returns a validated create-or-ask decision. It can ask up to
three questions per turn and continue across turns until requirements are clear.
The explicitly supplied name/description remain unchanged. Supplied prompts or
knowledge files switch the draft to transfer mode; they are not regenerated.
Generated content is identified as generated in the completion message.

Completed agents are saved in Custom agents immediately. The completion message
links to the refreshed list. Closing the composer mode or saying Cancel stops
creation. Ordinary chat resumes after completion or cancellation.

The composer uses `/api/agents/builder/chat`, which delegates to the authoritative
`/api/chat` route before coding, external
tool, image-analysis or bound-agent execution. The streaming entrypoint shares the
same route. Older backends reject the dedicated endpoint instead of treating a
creation request as an ordinary coding/chat task. Scope checks apply before state mutation. Definition creation, draft
state, conversation messages and idempotent results commit in one transaction.
Retries of the same submission reuse a request ID and do not create duplicate agents.

The regression suite covers the development-planner description, recovery of an
old text draft, question/answer context, and HTTP creation through the real builder
generation adapter. Model responses are stubbed in automated tests; these tests do
not establish real-model or real-GUI acceptance.
