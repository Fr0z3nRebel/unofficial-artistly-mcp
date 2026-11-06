# Read-only agent evaluation fixtures

`fixture.json` contains synthetic session, paginated design, and local job records.
`evaluation.xml` contains ten questions with expected answers derived only from those
records. The URLs and IDs are placeholders, not live Artistly resources.

These files are evaluation inputs, not an executable test suite. No fixture-backed MCP
server or evaluation runner is included. Running the regular server uses the configured
Artistly session; it does not automatically load this fixture.

For manual review, answer the questions using only `fixture.json`, then compare with
the XML answers. For an automated agent evaluation, a separate harness must expose
the fixture through read-only session, list, and get tool interfaces. It should return
the first page initially, the second for cursor `fixture-page-2`, and resolve each job’s
`design_uuids` against those pages. Apply any folder filters locally to fixture records.
Keep fixture data and credentials separate from the live client.

Do not invoke generation, upload, or download tools for these questions. References to
downloadable originals ask for metadata counts or file extensions, not saved files.
This evaluation checks browsing, pagination, job resolution, and readiness reasoning;
it does not validate generation, network integration, or image quality. Those behaviors
have separate automated tests and the live checks described in the main README.
