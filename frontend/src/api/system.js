import client from './client'

export async function fetchHealth() {
  const { data } = await client.get('/health')
  return data
}
