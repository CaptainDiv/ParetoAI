# Student Agents run as Gemini CLI sessions, not Claude Code or a custom agent

Entire (a mandatory hackathon partner tool) only auto-captures Checkpoints for two coding-agent CLIs today: Claude Code and Gemini CLI — there's no documented generic API for a custom agent to produce a real Checkpoint. We considered a custom Groq-driven tool-calling loop (zero cost, but Entire would have nothing real to attach to) and Claude Code (also Entire-supported, but the project's Anthropic key has no paid plan, so it would mean separate pay-as-you-go spend). We chose **Gemini CLI**: it's officially Entire-supported today and the user already has a free Gemini API key, so Student Agent Attempts produce genuine Checkpoints at no cost.

## Considered Options

- Custom Groq-based coding loop with a hand-rolled, best-effort Checkpoint-like record — rejected: not a real Entire integration, weak for judging criteria that weight Entire usage.
- Claude Code — rejected for this project: real Entire support, but requires paid Anthropic API spend the user wanted to avoid (see also the earlier Analyst/Curriculum/Judge switch from Anthropic to Groq).
