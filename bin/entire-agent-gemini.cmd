@echo off
rem entire-agent-gemini: Entire Agent Integration Protocol shim for Gemini CLI.
rem Add this bin/ directory to %PATH%, then run `entire agent add gemini`.
rem See pareto_ai/entire_agent_gemini.py (ADR-0004) for what this implements.
python "%~dp0..\pareto_ai\entire_agent_gemini.py" %*
