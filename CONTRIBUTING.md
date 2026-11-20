# Contributing

Thank you for your help. This file tells you how to make a change.

## Before you start

1. Find the milestone issue for the work. Each milestone issue has the label `milestone` and a checklist.
2. Open an issue first for a large change, so that we can agree on the design.

## Make a change

1. Make a branch from `main`.
2. Run `make check` before you push. It runs gofmt, vet, the tests and the linter.
3. Open a pull request. Give it one `area/*` label and one `type/*` label. Refer to the milestone issue.
4. Add one line to `CHANGELOG.md` under "Unreleased" for each change that a user can see.

## Writing style

Write all text (code comments, documents, issues, pull requests and commit messages) in plain technical English, as in ASD-STE100:

- Use short sentences. A descriptive sentence has at most 25 words. An instruction has at most 20 words.
- Use the active voice and the simple tenses.
- Write one instruction in each sentence.
- Do not use em dashes. Use a full stop, a comma or parentheses.
- Do not break a sentence over two lines in Markdown. One paragraph is one line.

## Commands that are not written yet

A command that is not written yet must exit with code 3 and name its milestone. Keep this true when you add a command.
