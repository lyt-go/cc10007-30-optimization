import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/baseline-switch'

// 核验码：本业务、构建上线与列表/刷新/浏览联动的端到端核对点。
const CODE = {
  business: '308a3eb1',
  buildOnline: '30c7bd46',
  backList: '3006f3b5',
  refresh: '30e04069',
  browse: '303b380f',
} as const

type SwitchRow = Record<string, string | number | boolean | string[] | null> & {
  id: number
  检测编号: string
  监控数据集: string
  旧基线版本: string
  新基线版本: string
  状态: string
  受影响场景数: number
  已完成召回数: number
  刷新失败场景: string[]
  样本不足: boolean
}
type CheckRow = Record<string, string | number | boolean | null>
type RecallRow = Record<string, string | number | boolean | null>
type SceneRow = Record<string, string | number | boolean | null>

const statusTone = (status: string) => {
  if (status.includes('失败')) return 'tone-danger'
  if (status.includes('不足') || status.includes('待复核')) return 'tone-warn'
  if (status.includes('核对')) return 'tone-ok'
  if (status.includes('生成')) return 'tone-info'
  return ''
}

export default function BaselineSwitchView() {
  const [switches, setSwitches] = useState<SwitchRow[]>([])
  const [checks, setChecks] = useState<CheckRow[]>([])
  const [recalls, setRecalls] = useState<RecallRow[]>([])
  const [scenes, setScenes] = useState<SceneRow[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [detectionNo, setDetectionNo] = useState('DATA-0001')
  const [message, setMessage] = useState('')
  const [errorMessage, setErrorMessage] = useState('')
  const [breakpoint, setBreakpoint] = useState<Record<string, unknown> | null>(null)

  const activeSwitch = useMemo(
    () => switches.find((item) => item.id === activeId) ?? null,
    [switches, activeId],
  )

  const reload = useCallback(async () => {
    setErrorMessage('')
    try {
      const [swRes, ckRes, rcRes, scRes] = await Promise.all([
        request(`${ENDPOINT}/switches`),
        request(`${ENDPOINT}/checks`),
        request(`${ENDPOINT}/recalls`),
        request(`${ENDPOINT}/scenes`),
      ])
      if (!swRes.ok || !ckRes.ok || !rcRes.ok || !scRes.ok) {
        throw new Error('推荐基线切换数据读取失败')
      }
      const swJson = await swRes.json()
      const ckJson = await ckRes.json()
      const rcJson = await rcRes.json()
      const scJson = await scRes.json()
      setSwitches(swJson.items ?? [])
      setChecks(ckJson.items ?? [])
      setRecalls(rcJson.items ?? [])
      setScenes(scJson.items ?? [])
      setActiveId((prev) => prev ?? (swJson.items[0]?.id ?? null))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '推荐基线切换数据读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  async function post(path: string, body?: Record<string, unknown>) {
    const response = await request(path, {
      method: 'POST',
      body: JSON.stringify(body ?? { values: {} }),
    })
    return response.json()
  }

  // 登记基线切换：由数据漂移待处理明细驱动，按场景版本差异重载。
  async function registerSwitch() {
    setErrorMessage('')
    const payload = await post(`${ENDPOINT}/switches`, { values: { 检测编号: detectionNo } })
    if (!payload.ok) {
      setErrorMessage(payload.message)
      return
    }
    setMessage(payload.message)
    setActiveId(payload.entry?.切换记录?.id ?? activeId)
    await reload()
  }

  async function retryFailed(row: SwitchRow) {
    const payload = await post(`${ENDPOINT}/switches/${row.id}/retry`, {
      values: { 恢复故障: true },
    })
    if (!payload.ok) setErrorMessage(payload.message)
    else setMessage(payload.message)
    await reload()
  }

  async function verify(row: SwitchRow) {
    const payload = await post(`${ENDPOINT}/switches/${row.id}/verify`, { values: {} })
    setMessage(payload.message)
    if (!payload.ok) setErrorMessage(payload.message)
    await reload()
  }

  async function syncRecommend() {
    const payload = await post(`${ENDPOINT}/sync-recommend`, { values: { 检测编号: detectionNo } })
    if (!payload.ok) setErrorMessage(payload.message)
    else setMessage(payload.message)
    await reload()
  }

  async function connectDb() {
    const payload = await post(`${ENDPOINT}/deploy/connect-db`, { values: {} })
    setMessage(payload.message)
  }

  async function buildOnline() {
    const payload = await post(`${ENDPOINT}/build-online`, { values: {} })
    if (!payload.ok) setErrorMessage(payload.message)
    else {
      setMessage(`${payload.message}（核验码 ${CODE.buildOnline}）`)
    }
  }

  async function browseRelated(row: SwitchRow) {
    setActiveId(row.id)
    const response = await request(`${ENDPOINT}/switches/${row.id}/breakpoint`)
    const payload = await response.json()
    setBreakpoint(payload)
    setMessage(`已浏览关联结果（核验码 ${CODE.browse}）：版本断点 ${payload.版本断点 ?? '—'}`)
  }

  // 核验码 30e04069：刷新页面后回到列表（3006f3b5）。
  function refreshAndBackToList() {
    setBreakpoint(null)
    setActiveId(null)
    void reload()
    setMessage(`已刷新并回到列表（核验码 ${CODE.refresh} → ${CODE.backList}）`)
  }

  const activeChecks = useMemo(
    () => checks.filter((item) => Number(item.切换记录id) === activeId),
    [checks, activeId],
  )
  const activeRecalls = useMemo(() => {
    const dataset = activeSwitch?.监控数据集
    return recalls.filter((item) => !dataset || item.监控数据集 === dataset)
  }, [recalls, activeSwitch])

  return (
    <section className="page" data-module="baseline-switch">
      <header className="page-head">
        <div>
          <h2>推荐基线切换核对</h2>
          <p className="page-desc">
            按场景版本差异重载：仅刷新引用新基线的场景，未引用场景保留原召回与多样性权重（核验码 {CODE.business}）。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn ghost" type="button" onClick={refreshAndBackToList}>
            刷新回列表
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出切换核对数据
          </button>
        </div>
      </header>

      <div className="filter-bar">
        <label className="filter-item">
          <span>数据漂移检测编号</span>
          <input value={detectionNo} onChange={(event) => setDetectionNo(event.target.value)} placeholder="DATA-0001" />
        </label>
        <button className="btn primary" type="button" onClick={() => void registerSwitch()}>
          登记基线切换
        </button>
        <button className="btn" type="button" onClick={() => void syncRecommend()}>
          同步推荐状态到漂移清单
        </button>
        <button className="btn ghost" type="button" onClick={() => void connectDb()}>
          联通联调环境数据库
        </button>
        <button className="btn" type="button" onClick={() => void buildOnline()}>
          智能推荐构建上线
        </button>
      </div>

      <h3>基线版本切换记录</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>检测编号</th>
            <th>监控数据集</th>
            <th>旧→新基线</th>
            <th>切换点</th>
            <th>受影响/完成</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {switches.map((row) => (
            <tr key={row.id}>
              <td>{row.检测编号}</td>
              <td>{row.监控数据集}</td>
              <td>{String(row.旧基线版本)} → {String(row.新基线版本)}</td>
              <td>{String(row.切换点)}</td>
              <td>{Number(row.受影响场景数)} / {Number(row.已完成召回数)}</td>
              <td><span className={statusTone(String(row.状态))}>{String(row.状态)}</span></td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => setActiveId(row.id)}>核对表</button>
                {Array.isArray(row.刷新失败场景) && row.刷新失败场景.length > 0 && (
                  <button className="link" type="button" onClick={() => void retryFailed(row)}>重试刷新</button>
                )}
                <button className="link" type="button" onClick={() => void verify(row)}>核对</button>
                <button className="link" type="button" onClick={() => void browseRelated(row)}>关联结果</button>
              </td>
            </tr>
          ))}
          {switches.length === 0 && (
            <tr><td className="empty-state" colSpan={7}>暂无切换记录，请先登记 DATA-0001 基线切换</td></tr>
          )}
        </tbody>
      </table>

      {activeSwitch && (
        <>
          <h3>推荐基线切换核对表（切换记录 #{activeSwitch.id}）</h3>
          <table className="data-table">
            <thead>
              <tr>
                <th>推荐场景</th>
                <th>旧召回批次号</th>
                <th>新召回批次号</th>
                <th>旧/新召回边界</th>
                <th>多样性权重变化</th>
                <th>漂移值</th>
                <th>监控样本</th>
                <th>状态</th>
                <th>可结算</th>
              </tr>
            </thead>
            <tbody>
              {activeChecks.map((row) => (
                <tr key={String(row.id)}>
                  <td>{String(row.推荐场景)}</td>
                  <td>{String(row.旧召回批次号 || '—')}</td>
                  <td>{String(row.新召回批次号 || '—')}</td>
                  <td>{String(row.旧召回边界 ?? '—')} / {String(row.新召回边界 ?? '—')}</td>
                  <td>
                    {String(row.旧多样性权重)} → {String(row.新多样性权重)}
                    （{String(row.多样性权重变化)}）
                  </td>
                  <td>{String(row.漂移值)}</td>
                  <td>{row.监控样本完整 ? '完整' : '不足'}</td>
                  <td><span className={statusTone(String(row.状态))}>{String(row.状态)}</span></td>
                  <td>{row.可结算 ? '是' : '否'}</td>
                </tr>
              ))}
              {activeChecks.length === 0 && (
                <tr><td className="empty-state" colSpan={9}>该切换暂无核对明细</td></tr>
              )}
            </tbody>
          </table>

          <h3>召回批次（旧批次只读，当前可用性唯一）</h3>
          <table className="data-table">
            <thead>
              <tr>
                <th>召回批次号</th>
                <th>推荐场景</th>
                <th>基线版本</th>
                <th>召回边界</th>
                <th>只读</th>
                <th>当前可用性</th>
                <th>刷新状态</th>
              </tr>
            </thead>
            <tbody>
              {activeRecalls.map((row) => (
                <tr key={String(row.id)}>
                  <td>{String(row.召回批次号)}</td>
                  <td>{String(row.推荐场景)}</td>
                  <td>{String(row.基线版本)}</td>
                  <td>{String(row.召回边界)}</td>
                  <td>{row.只读 ? '是' : '否'}</td>
                  <td>{row.当前可用性 ? '当前可用' : '—'}</td>
                  <td>{String(row.刷新状态)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <h3>推荐场景与多样性权重（未引用新基线的场景保持不变）</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>推荐场景</th>
            <th>引用数据集</th>
            <th>当前可用基线</th>
            <th>多样性权重</th>
            <th>监控样本完整</th>
          </tr>
        </thead>
        <tbody>
          {scenes.map((row) => (
            <tr key={String(row.id)}>
              <td>{String(row.推荐场景)}</td>
              <td>{String(row.引用数据集)}</td>
              <td>{String(row.当前可用基线)}</td>
              <td>{String(row.多样性权重)}</td>
              <td>{row.监控样本完整 ? '是' : '否（待复核）'}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {breakpoint && (
        <div className="stat-card">
          <h3>版本断点复核（{String((breakpoint as { 检测编号?: string }).检测编号)}）</h3>
          <p>版本断点：{String((breakpoint as { 版本断点?: string }).版本断点 ?? '—')}；
            仅向前更新：{String((breakpoint as { 仅向前更新?: boolean }).仅向前更新)}；
            可解释：{String((breakpoint as { 可解释?: boolean }).可解释)}</p>
          <p>冲突：{String((breakpoint as { 冲突?: string }).冲突 || '无')}</p>
        </div>
      )}

      <footer className="page-foot">
        <span>
          流程核验码：业务 {CODE.business} ｜ 构建上线 {CODE.buildOnline} ｜
          返回列表 {CODE.backList} ｜ 刷新 {CODE.refresh} ｜ 浏览关联 {CODE.browse}
        </span>
        {message && <span className="error-text" style={{ color: '#1a7f37' }}>{message}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
