import client from './client'

/**
 * Reports.
 *
 * Every report returns the same envelope — `{columns, rows, totals, meta}` —
 * so there is one fetch function and one download function here rather than
 * thirteen. A report added on the server appears in the UI by adding a row
 * to the catalogue in ReportsView, not by writing any new plumbing.
 *
 * All of them are admin-only; the API returns 403 for a sales executive
 * regardless of what the menu shows.
 */

export function fetchReport(path, params = {}) {
  return client.get(`/reports${path}`, { params }).then((r) => r.data)
}

/**
 * Save a report as CSV or XLSX.
 *
 * Fetched through axios rather than linked, because the access token lives
 * in memory and a plain <a href> would arrive without the auth header.
 */
export async function downloadReport(path, params, format, filename) {
  const response = await client.get(`/reports${path}`, {
    params: { ...params, format },
    responseType: 'blob',
  })
  const url = URL.createObjectURL(response.data)
  const link = document.createElement('a')
  link.href = url
  link.download = `${filename}.${format}`
  link.click()
  URL.revokeObjectURL(url)
}
