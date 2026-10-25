---
# Copy this folder to agents/<name>/ (or run `factory new agent <name>`).
# The folder name must equal `name`. Every key below is validated on load;
# mistakes show up as an error banner in the UI instead of crashing the factory.
name: _template
description: One line saying what this agent does
model: fast                  # alias from config/models.yaml (omit = global default)
role: general                # general | reviewer | security | tester | implementer
isolation_level: L0          # L0 no tools | L1 read-only | L2 read/write + sandbox | L3 + network
tools: []                    # e.g. [fs.read, fs.list, artifact.write]; must be [] at L0
fs_scope:                    # globs; {workspace} = this agent's worktree, {artifacts} = run artifacts
  read: []
  write: []
  deny: []                   # never writable, even if matched by `write`
  deny_read: []              # never readable, even if matched by `read`
inputs: [request.md]         # artifacts injected into the first message
outputs: [example.md]        # artifacts this agent must produce
max_iterations: 10           # LLM turns per invocation
max_tokens_budget: 100000    # tokens per invocation (input + output)
---
# Role
You are the <Name> agent in a software factory. State the single job this agent
owns, and what it must never do.

# Inputs
Describe each input artifact and how to use it.

# Process
1. Numbered, concrete steps.

# Output
Describe the exact structure of each output artifact.

# Quality bar
- Concrete, checkable criteria the output must meet.
