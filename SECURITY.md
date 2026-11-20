# Security

## Report a problem

Do not open a public issue for a security problem. Use the private reporting form of GitHub: open the Security tab of this repository and select "Report a vulnerability".

We reply in 7 days.

## Scope

`rh` downloads files from the internet and unpacks archives. A problem in these steps is in scope, for example a path that leaves the target directory, or a hash check that a file can skip.

The Lean source is not in scope here. Report a problem with the proof itself in [tamnd/riemann-lean](https://github.com/tamnd/riemann-lean).
