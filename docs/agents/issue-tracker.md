# Issue tracker: GitHub

Issues and specs live in GitHub Issues for `mclbautista/t6-core`.
Use `gh` from this clone; it infers the repository from `origin`.

## Conventions

- Create: `gh issue create --title "..." --body-file <path>`.
- Read a ticket and its discussion: `gh issue view <number> --comments`.
- Read metadata: `gh issue view <number> --json number,title,body,labels,assignees,state`.
- List: `gh issue list --state open --json number,title,body,labels,assignees`.
- Comment: `gh issue comment <number> --body-file <path>`.
- Apply/remove labels: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`.
- Close: `gh issue close <number> --comment "..."`.

When a skill says "publish to the issue tracker", create a GitHub issue.
When it says "fetch the relevant ticket", read the issue and its comments.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub shares issue and PR numbers. For an ambiguous reference, try
`gh pr view <number>` and fall back to `gh issue view <number>`.

## Wayfinding operations

- Map: one issue labelled `wayfinder:map`, containing Notes, Decisions-so-far, and Fog.
- Children: tickets labelled `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`, or `wayfinder:task`. Link them as GitHub sub-issues. If unavailable, use a task list in the map and `Part of #<map>` in each child.
- Blocking: use native GitHub issue dependencies. Retrieve the blocker's numeric database ID with `gh api repos/mclbautista/t6-core/issues/<number> --jq .id`. Add it with `gh api --method POST repos/mclbautista/t6-core/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`. If unavailable, record `Blocked by: #<number>` in the child.
- Frontier: the first open child in map order with no assignee or open blockers. Use `issue_dependencies_summary.blocked_by` for native dependencies; otherwise inspect the referenced blocker issues.
- Claim: `gh issue edit <number> --add-assignee @me`.
- Resolve: comment with the result, close the child, and add a context pointer to the map's Decisions-so-far.
