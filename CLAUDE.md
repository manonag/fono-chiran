# CHIRAN Integration

This repo is managed by CHIRAN, the COO orchestrator.

## At Session Start
1. Call generate_handoff with the appropriate project name to load context
2. Call list_tasks to see what is queued
3. If there is a task to work on, call claim_task with your session_id and the task_id

## During Work
- Call heartbeat_task periodically to keep your lock alive
- Call list_decisions if you need to check existing architectural decisions
- Call list_principles to check coding standards and rules
- Call record_decision if you make any technical decision during implementation

## At Session End
1. Call update_task with status and result to report what you did
2. Call save_session with a summary of what happened

## Project Mapping
- fono-backend uses project slug: fono
- fono-chiran uses project slug: chiran
- fono-frontend uses project slug: fono
- askben-ai uses project slug: askben

## Rules
- Never use em dashes
- Surgical patches only, never rewrite entire files
- Test locally before pushing (especially API integrations: test with curl first)
- Commit after each logical chunk
