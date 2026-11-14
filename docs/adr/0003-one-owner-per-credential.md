# 3. One owner per credential

Date: 2026-11-14. Status: accepted. Source: spec #178.

## Context

Apps have held their own outside-service credentials, duplicating clients and
policy. A single holder makes the boundary and responsibility for each
credential explicit.

## Decision

Each credential has one holder. The t6 API and its main worker form one Compose
project and together are the one holder for their credentials.

| Credential | Holder | What an app does instead |
| --- | --- | --- |
| Notion | t6 API and main worker | Call the t6 API for Notion work. |
| LLM providers | t6 API and main worker | Call `llm run` on the API with the task definition. |
| Google | t6 API and main worker | Call the t6 API for Google work. |
| ElevenLabs | Transcribe worker | Request transcription through t6. |
| Telegram | Gateway | Request Telegram transport through the gateway. |
| Frame.io | Frame.io worker | Request Frame.io work through t6. |

The API applies masking and the zero data retention (ZDR) flag to `llm run`;
apps supply their own prompt and output schema. Every new credential needs a
row in this record naming its holder and what apps do instead.

## Consequences

- Apps and the CLI do not need Notion, LLM provider or Google credentials.
- Credential-dependent work crosses a service boundary; the holder applies its
  policy and handles the outside service.
