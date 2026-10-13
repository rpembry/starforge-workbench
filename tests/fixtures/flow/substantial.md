# Tasks

<!-- FLOW work-item: wi-example-43 -->
<!-- Source: https://github.com/example-org/example-repo/issues/43 -->

## P1

- [ ] Add parser coverage
  - **ID**: parser-coverage-01
  - **Details**: Preserve this first line.
    Preserve this second line as well.
  - **Acceptance**: A reviewer can check the result.
  - **Custom field**: Retain this unknown value.
  - [ ] Check a nested case

- [ ] Repair parsing
  - **ID**: parser-repair-02
  - **Blocked by**: parser-coverage-01
  - **Blocked**: Waiting for an external review.
  - **Files**: `src/parser.py`

## P3

An ordinary note between tasks remains untouched.

```markdown
- [ ] This fenced example is not a task
  - **ID**: example-only
```
