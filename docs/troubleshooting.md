# Troubleshooting

Common issues and solutions when using `quill`.

---

## Connection Issues

### `quill check` fails for GitHub

**Symptom**: Error connecting to GitHub API.

**Solutions**:
- Verify your `GITHUB_TOKEN` environment variable is set: `echo $GITHUB_TOKEN`
- Check that the token hasn't expired — regenerate it at [github.com/settings/tokens](https://github.com/settings/tokens)
- If you're behind a corporate proxy, set `HTTPS_PROXY`: `export HTTPS_PROXY=http://proxy:8080`
- Without a token, GitHub limits you to 60 requests/hour. With a PAT, you get 5,000/hour.

### `quill check` fails for Jira

**Symptom**: Connection refused or authentication error.

**Solutions**:
- Verify your `JIRA_PAT` environment variable is set
- Verify the `jira.server` URL is correct and reachable: `curl -I https://jira.example.com`
- If using a self-signed certificate, set `verify_ssl: false` in the config
- PAT authentication requires Jira Server/Data Center 8.14+ or Jira Cloud
- Test the PAT directly:
  ```bash
  curl -s -H "Authorization: Bearer $JIRA_PAT" \
    "https://jira.example.com/rest/api/2/myself"
  ```

---

## Sync Issues

### Duplicate Jira issues are being created

**Cause**: The `github_link_field` is not set up correctly.

**Solutions**:
1. Verify the custom field ID matches your Jira instance (see [Jira Setup](jira-setup.md#step-2-find-the-custom-field-id))
2. Check that the custom field is associated with the correct Jira project screens
3. Verify existing synced issues have the field populated:
   ```bash
   curl -s -H "Authorization: Bearer $JIRA_PAT" \
     "https://jira.example.com/rest/api/2/search?jql=project=CAS+AND+cf[10200]+is+not+EMPTY" \
     | python3 -m json.tool | head -20
   ```

### Issues are being updated on every run (even when unchanged)

**Cause**: The content hash entity property is missing or being cleared.

**Solutions**:
- Verify the entity property is present on the Jira issue:
  ```bash
  curl -s -H "Authorization: Bearer $JIRA_PAT" \
    "https://jira.example.com/rest/api/2/issue/CAS-123/properties/quill-content-hash" \
    | python3 -m json.tool
  ```
  You should see the `quill-content-hash` property with a SHA256 value.

### State transitions are not working

**Symptom**: Warning log: `Transition 'Done' unavailable for CAS-123`.

**Solutions**:
- The transition names must match your Jira workflow. Quill tries "Done", "Closed", and "Resolved" in order.
- List available transitions for an issue:
  ```bash
  curl -s -H "Authorization: Bearer $JIRA_PAT" \
    "https://jira.example.com/rest/api/2/issue/CAS-123/transitions" \
    | python3 -m json.tool
  ```
- If your workflow uses different names, note the available transitions from the log output. Custom transition name support is planned for a future release.

### Comments are duplicated

**Cause**: quill deduplicates comments by checking if the GitHub comment URL appears in any existing Jira comment. If comments are being edited or the Jira comment format changed, duplicates may occur.

**Solution**: This is a known limitation. For now, manually delete the duplicate Jira comments.

---

## Rate Limits

### GitHub rate limit exceeded

**Symptom**: `403 rate limit exceeded` errors.

**Solutions**:
- Use a GitHub PAT (`GITHUB_TOKEN`) — anonymous access is limited to 60 req/hr
- Set the `since` filter in your config to limit the backfill window
- Filter by `labels` to reduce the number of issues fetched
- Increase the sync interval (e.g., every 4 hours instead of every hour)

### Jira API throttling

**Symptom**: `429 Too Many Requests` or slow responses.

**Solutions**:
- Reduce `batch_size` in the config
- Increase the sync interval
- Contact your Jira admin about API rate limits on your instance

---

## Environment Issues

### `quill: command not found`

**Solutions**:
- If using Pixi: `pixi run quill sync`
- If using pip: make sure you installed with `pip install -e .` and the virtualenv is activated
- Check that the entry point is registered: `pip show quill`

### `ModuleNotFoundError: No module named 'quill'`

**Solutions**:
- Install in editable mode: `pip install -e .` or `pixi install`
- Verify the `src/quill/` directory exists with `__init__.py`

### TOML parsing errors

**Solutions**:
- Validate your TOML syntax: `python3 -c "import tomllib; tomllib.load(open('quill.toml', 'rb'))"` (Requires Python 3.11+)
- Ensure string values are correctly quoted in TOML
- Ensure environment variables referenced with `${VAR}` are actually set

---

## Getting Help

If you encounter an issue not covered here:

1. Run with `--dry-run` to see what quill would do without making changes
2. Check the logs — quill uses Rich-formatted logging with timestamps and tracebacks
3. Open an issue on the GitHub repository with the error message and your (redacted) config
