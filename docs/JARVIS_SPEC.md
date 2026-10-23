# JARVIS spec (owner's requirements, 2026-10-23)

A voice assistant and agentic AI for the owner's Windows desktop.

## UI
- Polished, futuristic JARVIS interface.
- Dark cinematic black/charcoal background; cyan, electric-blue and blue-white holographic
  glow; premium glassmorphism. No green-tinted theme.
- Animated central orb with circular HUD elements and subtle animations.
- Header: "JARVIS — Your personal AI assistant".
- Command input, send button, and voice activation control.
- AI provider, network, and system status.
- Listening, thinking, speaking, idle, and offline states.
- Analyze, Assist, and Execute controls with real functionality.
- Clean, professional layout; responsive and lightweight on Intel Iris Xe.

## Providers
- Common provider interface. Groq primary online, Gemini secondary online, Ollama offline.
- Configurable model names and provider priority.
- Network and provider availability detection.
- Bounded retries and timeouts; graceful fallback and recovery.
- Conversation history persisted in SQLite.
- Clear error reporting and a responsive UI.
- API keys only through environment variables.
- Never pretend a model was used if it was not.

## Windows automation
- Safe, modular tools: approved applications and URLs, keyboard shortcuts, dictated typing,
  and other narrowly scoped desktop actions.
- Low risk: appropriate read-only actions. Medium risk: confirmation for meaningful side
  effects. High risk: strong confirmation or refusal for destructive or sensitive actions.
- Never execute arbitrary AI-generated shell commands. Restrict subprocesses, filesystem
  access, and system operations. Do not bypass security controls or silently alter Windows
  settings.
