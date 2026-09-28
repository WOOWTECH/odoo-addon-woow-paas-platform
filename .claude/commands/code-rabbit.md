---
allowed-tools: Agent, Read, Edit, Write, Grep
---

# CodeRabbit Review Handler

Process CodeRabbit review comments with context-aware discretion.

## Usage
```
/code-rabbit
```

Then paste one or more CodeRabbit comments.

## Instructions

### 1. Context

CodeRabbit sees the diff, not the whole codebase. Judge each comment against this repo's architecture and conventions, then accept or ignore it and say why in one line.

### 2. Process Comments

#### Single File Comments
If all comments relate to one file:
- Read the file for context
- Evaluate each suggestion
- Apply accepted changes with Edit
- Report which suggestions were accepted/ignored and why

#### Multiple File Comments
If comments span multiple files:

Launch parallel sub-agents with the Agent tool:
```yaml
Agent:
  description: "CodeRabbit fixes for {filename}"
  subagent_type: "general-purpose"
  prompt: |
    Review and apply CodeRabbit suggestions for {filename}.
    
    Comments to evaluate:
    {relevant_comments_for_this_file}
    
    Instructions:
    1. Read the file to understand context
    2. For each suggestion:
       - Evaluate validity given codebase patterns
       - Accept if it improves quality/correctness
       - Ignore if not applicable
    3. Apply accepted changes using Edit
    4. Return summary:
       - Accepted: {list with reasons}
       - Ignored: {list with reasons}
       - Changes made: {brief description}
    
    Use discretion - CodeRabbit lacks full context.
```

### 3. Consolidate Results

After all sub-agents complete:
```
📋 CodeRabbit Review Summary

Files Processed: {count}

Accepted Suggestions:
  {file}: {changes_made}
  
Ignored Suggestions:
  {file}: {reason_ignored}

Overall: {X}/{Y} suggestions applied
```

### 4. Common Patterns to Ignore

- **Style preferences** that conflict with project conventions
- **Generic best practices** that don't apply to our specific use case
- **Performance optimizations** for code that isn't performance-critical
- **Accessibility suggestions** for internal tools
- **Security warnings** for already-validated patterns
- **Import reorganization** that would break our structure

### 5. Common Patterns to Accept

- **Actual bugs** (null checks, error handling)
- **Security vulnerabilities** (unless false positive)
- **Resource leaks** (unclosed connections, memory leaks)
- **Type safety issues** (TypeScript/type hints)
- **Logic errors** (off-by-one, incorrect conditions)
- **Missing error handling** 

## Decision Framework

For each suggestion, consider:
1. **Is it correct?** - Does the issue actually exist?
2. **Is it relevant?** - Does it apply to our use case?
3. **Is it beneficial?** - Will fixing it improve the code?
4. **Is it safe?** - Could the change introduce problems?

Only apply if all answers are "yes" or the benefit clearly outweighs risks.
