import time
import logging

logger = logging.getLogger(__name__)

class JiraConsistencyMixin(object):
    """
    Mixin to patch Jira client for read-after-write consistency.
    Keeps a short-lived cache of recent issue keys and ensures search
    results always include recent writes (and exclude recent deletes),
    even if Jira's index hasn't caught up.
    """

    _recent_writes = None
    _recent_deletes = None
    _cache_ttl = 60 * 10  # 10 minutes

    def __init__(self, *args, **kwargs):
        super(JiraConsistencyMixin, self).__init__(*args, **kwargs)
        self._recent_writes = {}   # issue_key -> timestamp
        self._recent_deletes = {}  # issue_key -> timestamp

    def _record_write(self, issue_key):
        logger.debug("Recorded WRITE for issue %s", issue_key)
        self._recent_writes[issue_key] = time.time()
        # if it was marked deleted, clear it
        self._recent_deletes.pop(issue_key, None)

    def _record_delete(self, issue_key):
        logger.debug("Recorded DELETE for issue %s", issue_key)
        self._recent_deletes[issue_key] = time.time()
        # if it was in writes, clear it
        self._recent_writes.pop(issue_key, None)

    def _prune_cache(self):
        cutoff = time.time() - self._cache_ttl
        self._recent_writes = {
            k: t for k, t in self._recent_writes.items() if t >= cutoff
        }
        self._recent_deletes = {
            k: t for k, t in self._recent_deletes.items() if t >= cutoff
        }

    # --- Override write ops ---

    def create_issue(self, fields, *args, **kwargs):
        issue = super(JiraConsistencyMixin, self).create_issue(fields, *args, **kwargs)
        key = issue["key"] if isinstance(issue, dict) else issue.key
        logger.debug("Created issue %s", key)
        self._record_write(key)
        return issue

    def update_issue(self, issue, data):
        issue_key = issue.key
        result = super(JiraConsistencyMixin, self).update_issue(issue, data)
        logger.debug("Updated issue %s", issue_key)
        self._record_write(issue_key)
        return result

    def delete_issue(self, issue_id_or_key, *args, **kwargs):
        if '-' not in str(issue_id_or_key):  # it's an id
            issue_key = super(JiraConsistencyMixin, self).issue(issue_id_or_key).key
        else:
            issue_key = issue_id_or_key

        issue = super(JiraConsistencyMixin, self).issue(issue_id_or_key)
        result = super(JiraConsistencyMixin, self).delete_issue(issue_key, *args, **kwargs)
        logger.debug("Deleted issue %s", issue.key)
        self._record_delete(issue.key)
        return result

    # --- Override search op ---

    def search_issues(self, jql_str, *args, **kwargs):
        self._prune_cache()
        search = super(JiraConsistencyMixin, self).search_issues(jql_str, *args, **kwargs)

        # Normalize result format (dict vs list of resources)
        if isinstance(search, dict):
            issues = search.get("issues", [])
            found_keys = set([issue["key"] for issue in issues])
        else:
            issues = search
            found_keys = set([issue.key for issue in issues])

        # Add missing recent writes
        missing = [k for k in self._recent_writes.keys() if k not in found_keys]
        if missing:
            logger.debug("Patching search: fetching %s missing recent writes", missing)
            fetched = [super(JiraConsistencyMixin, self).issue(k) for k in missing]
            if isinstance(search, dict):
                search["issues"].extend(fetched)
            else:
                search.extend(fetched)

        # Remove recent deletes
        deleted = set(self._recent_deletes.keys())
        if deleted:
            logger.debug("Patching search: removing recently deleted issues %s", deleted)
            if isinstance(search, dict):
                search["issues"] = [
                    issue for issue in search["issues"]
                    if (issue["key"] if isinstance(issue, dict) else issue.key) not in deleted
                ]
            else:
                search[:] = [issue for issue in search if issue.key not in deleted]

        return search
