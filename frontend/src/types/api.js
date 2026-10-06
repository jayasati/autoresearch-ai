/**
 * Shapes of the backend's responses, as JSDoc typedefs.
 *
 * This project stays in plain JavaScript (see DEVELOPMENT_LOG, stage 3), so these
 * are documentation that editors can still use for completion and hover, rather
 * than types the build enforces. They mirror `backend/app/schemas/common.py`.
 *
 * Import them where useful:
 *   `@type {import('../types/api.js').HealthResponse}`
 */

/**
 * `GET /api/health`
 * @typedef {object} HealthResponse
 * @property {'ok'} status
 * @property {string} version
 * @property {string} environment
 */

/**
 * `GET /`
 * @typedef {object} ServiceInfoResponse
 * @property {string} name
 * @property {string} version
 * @property {string} environment
 * @property {string} description
 * @property {string} stage      Which build stage of the project is live.
 * @property {string} docs_url
 * @property {string} health_url
 * @property {string} api_version_prefix
 */

/**
 * `GET /api/v1/system/capabilities`
 * @typedef {object} CapabilitiesResponse
 * @property {string[]} modes                       Mirrors ResearchMode.
 * @property {Record<string, boolean>} integrations Which have usable credentials.
 * @property {string[]} missing_credentials         Env var names still to set.
 * @property {string[]} implemented                 Research capabilities that exist.
 */

/**
 * The body of every error response.
 *
 * `code` is stable and safe to branch on; `message` is for humans and may be
 * reworded. `request_id` appears in every backend log line for the same request.
 *
 * @typedef {object} ErrorDetail
 * @property {string} code
 * @property {string} message
 * @property {Record<string, unknown>} details
 * @property {string} request_id
 */

/**
 * @typedef {object} ErrorResponse
 * @property {ErrorDetail} error
 */

export {}
