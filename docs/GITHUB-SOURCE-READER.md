# GitHub source files and directories

Reach's github_api backend accepts GitHub blob/tree URLs at an explicit commit, branch or tag. Directory reads return JSON with ref, path and entries (name, path, type, SHA and a link retaining that ref). All entries returned by the API are retained instead of the comment preview's fifty-item limit. GitHub's Contents API caps directories at 1,000 entries; a response reaching that limit is marked truncated, not certified complete. This reader is non-recursive. An encoded slash can identify a ref containing a slash.

File reads decode Base64 as UTF-8 without normalizing newlines. The object media type supports the large-file metadata response; encoding none falls back to the Git blob identified by the returned SHA. Empty files are valid. Binary, unavailable or invalidly encoded content produces an explicit backend failure, and the existing channel fallback still reports the backend that actually answered. No upstream repository is cloned or saved as a source directory.

The normal reach_read preview stays bounded. When Reach itself shortens text, full_text retains the exact received source before the existing tool-result offloader runs. read_artifact can then query later definitions with occurrence and context_chars instead of repeating downloads. An upstream truncated response stays marked truncated; this feature cannot restore bytes already absent upstream. Short results, repository README reads and issue/PR behavior keep their existing shapes.

This uses the existing GitHub API credentials/public access, channel routing, artifact ownership and result offloader. No new tool, parameter, permission or remote service is required. The reach_read description advertises source URLs and artifact follow-up.

## Validation and scope

Silver task1755 follows native trial1750: 577.37 seconds, 46 tools, 22 generic fetches, unsupported directory URL and guessed filenames. That trial demonstrated the occurrence reader but did not establish efficient assistance. This change addresses those observed retrieval failures; overall research efficiency must still be measured in a new native task.

A thirteen-method external contract was frozen before implementation. R1 incorrectly expected an integer issue number; R2 changes only that expectation to the existing string, with both versions and hashes retained. Corrected RED: one failure/eight errors; implementation GREEN: thirteen pass. The focused regression run passes 78 tests (Reach backends/tools/router and artifact readers/offloader). A live backend read finds all thirteen entries in the pinned Platinum dataproc/src directory and reads speciesproc.c (42,639 UTF-8 bytes, SHA256 3bf83de24379804c4f36681f671b890cd411ad1d65184434185a17a0fcb4b63e) in two requests and 1.562 seconds, with no source file saved. This backend measurement is not a native model turn or a general speedup claim. Native results remain to be measured; fixture success alone is not a claim of efficient end-to-end research.

API behavior: [repository contents](https://docs.github.com/en/rest/repos/contents#get-repository-content), [Git blobs](https://docs.github.com/en/rest/git/blobs#get-a-blob). Files above the upstream 100 MB limit and directories beyond the non-recursive Contents API limit are outside this delivery.
