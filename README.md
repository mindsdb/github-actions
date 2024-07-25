# github-actions

Use an action from this repo in your workflow like this:

```
- name: Pull MindsDB Github Actions
  uses: actions/checkout@v4
  with:
    repository: mindsdb/github-actions
    path: github-actions
    ssh-key: ${{ secrets.GH_ACTIONS_PULL_SSH }}
- uses: ./github-actions/<action-name>
```

**NOTE: This needs to go AFTER any `actions/checkout` step for the current repo**