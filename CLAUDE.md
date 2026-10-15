# CLAUDE.md

## Secrets

- Never read `.env` files (for example `backend/.env`). They hold real API keys.
- Do not open them with Read, `cat`, `grep`, `head`, or any other tool or command, and do not print their values.
- Use `backend/.env.example` to see which variables exist. If you need to know whether a variable is set, ask the user.
