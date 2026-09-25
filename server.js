import { timingSafeEqual } from 'node:crypto'
import { mkdir } from 'node:fs/promises'
import { createServer, request } from 'node:http'
import { connect } from 'node:net'
import { join, resolve } from 'node:path'
import { spawn } from 'node:child_process'

const railway = Boolean(process.env.RAILWAY_ENVIRONMENT)
const base = process.env.RAILWAY_VOLUME_MOUNT_PATH || (railway ? '/data' : process.cwd())
const home = resolve(process.env.DSH_HOME || join(base, '.dsh'))
const workspace = resolve(process.env.HARNESS_WORKSPACE || join(home, 'workspace'))
const port = Number(process.env.PORT || 8080)
const webPort = 3080
const publicHost = (process.env.RAILWAY_PUBLIC_DOMAIN || 'harness-pi.up.railway.app').trim().toLowerCase()
const rawPassword = (process.env.Senha_Acesso || '').trim()
const pairedQuotes = [['"', '"'], ["'", "'"], ['“', '”'], ['‘', '’']]
const password = rawPassword.length >= 2 && pairedQuotes.some(([open, close]) => rawPassword.startsWith(open) && rawPassword.endsWith(close))
  ? rawPassword.slice(1, -1)
  : rawPassword
const key = process.env.Chave_SK || ''

if (!password) throw new Error('Senha_Acesso não está configurada.')
if (!key) throw new Error('Chave_SK não está configurada.')
if (!/^[a-z0-9.-]+(?::\d+)?$/u.test(publicHost)) throw new Error('RAILWAY_PUBLIC_DOMAIN inválido.')
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('PORT inválida.')

await mkdir(workspace, { recursive: true })

const launch = { token: '' }
const ready = { listening: false }
const cookiePrefix = 'dsh-auth-'
const dsh = spawn(resolve('node_modules/.bin/dsh'), [
  'web', '--no-open', '--port', String(webPort), '--trusted-host', publicHost,
], {
  cwd: workspace,
  env: { ...process.env, DSH_HOME: home, DEEPSEEK_API_KEY: key },
  stdio: ['ignore', 'pipe', 'pipe'],
})

function capture(source, target) {
  let pending = ''
  source.setEncoding('utf8')
  source.on('data', chunk => {
    const lines = (pending + chunk).split(/\r?\n/u)
    pending = lines.pop() || ''
    for (const raw of lines) {
      const line = raw.replace(/\u001b\[[0-9;]*m/gu, '')
      const match = line.match(/[?&]token=([^&\s)]+)/u)
      if (match) {
        launch.token = decodeURIComponent(match[1])
        if (!ready.listening) {
          ready.listening = true
          server.listen(port, '0.0.0.0', () => console.log(`HarnessPI privado pronto na porta ${port}.`))
        }
      }
      const clean = line.replace(/https?:\/\/[^\s)]+\?token=[^\s)]+/gu, '[URL privada]')
      if (clean.trim()) target.write(clean + '\n')
    }
  })
}

capture(dsh.stdout, process.stdout)
capture(dsh.stderr, process.stderr)
dsh.on('error', error => {
  console.error(`Não foi possível iniciar o Harness: ${error.message}`)
  process.exitCode = 1
})

function equal(a, b) {
  const left = Buffer.from(a)
  const right = Buffer.from(b)
  if (left.length !== right.length) {
    timingSafeEqual(Buffer.alloc(right.length), right)
    return false
  }
  return timingSafeEqual(left, right)
}

function authorized(header = '') {
  const [scheme, encoded] = header.split(/\s+/u)
  if (scheme?.toLowerCase() !== 'basic' || !encoded) return false
  const value = Buffer.from(encoded, 'base64').toString('utf8')
  const split = value.indexOf(':')
  return split >= 0 && value.slice(0, split) === 'HarnessPI' && equal(value.slice(split + 1), password)
}

function hasNativeCookie(header = '') {
  return header.split(';').some(part => part.trim().startsWith(cookiePrefix))
}

function reject(res) {
  res.writeHead(401, {
    'www-authenticate': 'Basic realm="HarnessPI", charset="UTF-8"',
    'cache-control': 'no-store',
    'content-length': '0',
  })
  res.end()
}

function requestUrl(req) {
  try {
    return new URL(req.url || '/', `http://${req.headers.host || publicHost}`)
  } catch {
    return new URL('/', `http://${publicHost}`)
  }
}

function proxyRequest(req, res) {
  const headers = { ...req.headers }
  delete headers.authorization
  const upstream = request({
    host: '127.0.0.1',
    port: webPort,
    method: req.method,
    path: req.url,
    headers,
  }, response => {
    const headers = { ...response.headers }
    if (railway && Array.isArray(headers['set-cookie'])) {
      headers['set-cookie'] = headers['set-cookie'].map(value => /;\s*secure(?:;|$)/iu.test(value) ? value : `${value}; Secure`)
    }
    res.writeHead(response.statusCode || 502, headers)
    response.pipe(res)
  })
  upstream.on('error', () => {
    if (!res.headersSent) res.writeHead(502, { 'cache-control': 'no-store' })
    res.end('Harness indisponível.')
  })
  req.on('aborted', () => upstream.destroy())
  req.pipe(upstream)
}

const server = createServer((req, res) => {
  if (!authorized(req.headers.authorization)) return reject(res)

  const url = requestUrl(req)
  if (url.pathname === '/harness-pi' || url.pathname === '/harness-pi/') {
    res.writeHead(302, { location: '/', 'cache-control': 'no-store' })
    res.end()
    return
  }
  if (url.pathname === '/' && req.method === 'GET' && !url.searchParams.has('token')
    && !hasNativeCookie(req.headers.cookie)) {
    if (!launch.token) {
      res.writeHead(503, { 'retry-after': '2', 'cache-control': 'no-store' })
      res.end('Harness iniciando.')
      return
    }
    res.writeHead(302, { location: `/?token=${encodeURIComponent(launch.token)}`, 'cache-control': 'no-store' })
    res.end()
    return
  }
  proxyRequest(req, res)
})

const sockets = new Set()
server.on('connection', socket => {
  sockets.add(socket)
  socket.once('close', () => sockets.delete(socket))
})

server.on('upgrade', (req, socket, head) => {
  if (!authorized(req.headers.authorization)) {
    socket.end('HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Basic realm="HarnessPI", charset="UTF-8"\r\nCache-Control: no-store\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
    return
  }
  const upstream = connect(webPort, '127.0.0.1', () => {
    const headers = []
    for (let i = 0; i < req.rawHeaders.length; i += 2) {
      if (req.rawHeaders[i].toLowerCase() !== 'authorization') {
        headers.push(`${req.rawHeaders[i]}: ${req.rawHeaders[i + 1]}`)
      }
    }
    upstream.write(`${req.method} ${req.url} HTTP/${req.httpVersion}\r\n${headers.join('\r\n')}\r\n\r\n`)
    if (head.length) upstream.write(head)
    socket.pipe(upstream)
    upstream.pipe(socket)
  })
  upstream.on('error', () => socket.destroy())
  socket.on('close', () => upstream.destroy())
})

let stopping = false
function stop() {
  if (stopping) return
  stopping = true
  if (server.listening) server.close()
  for (const socket of sockets) socket.destroy()
  dsh.kill('SIGTERM')
  const force = setTimeout(() => dsh.kill('SIGKILL'), 6000)
  force.unref()
}

process.once('SIGINT', stop)
process.once('SIGTERM', stop)
dsh.once('close', (code, signal) => {
  if (!stopping) {
    console.error(`O Harness encerrou (${signal || code}).`)
    process.exitCode = typeof code === 'number' && code !== 0 ? code : 1
    stop()
  }
})
