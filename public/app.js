'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = n => n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KiB' : n < 1073741824 ? (n / 1048576).toFixed(1) + ' MiB' : (n / 1073741824).toFixed(2) + ' GiB';
const phone = /Android|iPhone|iPad|Mobile/i.test(navigator.userAgent);
const platform = /iPhone|iPad/.test(navigator.userAgent) ? 'iPhone / iPad' : /Android/.test(navigator.userAgent) ? 'Android 手机' : /Windows/.test(navigator.userAgent) ? 'Windows 电脑' : /Mac/.test(navigator.userAgent) ? 'Mac 电脑' : '我的设备';
function readStore(store, key, fallback) { try { return store.getItem(key) || fallback; } catch (_) { return fallback; } }
function writeStore(store, key, value) { try { value ? store.setItem(key, value) : store.removeItem(key); } catch (_) {} }
let token = readStore(localStorage, 'linqdrop-v2-token', '');
let name = readStore(localStorage, 'linqdrop-v2-name', platform);
let config = {}, me = null, peers = [], transfers = [], selected = '', selectedUrl = '', filter = 'all';
let syncTimer, toastTimer, busyCount = 0, currentOffer = '', syncCount = 0, deviceSignature = '', stopped = false;
const localFiles = new Map(), hidden = new Set(), notified = new Set();

function toast(text) { $('toast').textContent = text; $('toast').classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.remove('show'), 4200); }
function banner(text) { $('connection-banner').textContent = text; $('connection-banner').hidden = !text; }
function requestId() { if (window.crypto && typeof crypto.getRandomValues === 'function') return Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join(''); return Date.now().toString(36) + Math.random().toString(36).slice(2); }

async function api(path, options = {}) {
  const controller = new AbortController();
  let timedOut = false;
  const external = options.signal;
  const cancel = () => controller.abort();
  if (external) { if (external.aborted) cancel(); else external.addEventListener('abort', cancel, {once:true}); }
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, options.timeout || 15000);
  const headers = {};
  if (token && options.auth !== false) headers.Authorization = 'Bearer ' + token;
  if (options.admin && config.adminKey) headers['X-LinqDrop-Admin'] = config.adminKey;
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  try {
    const response = await fetch(path, {method:options.method || 'GET', headers, cache:'no-store', signal:controller.signal,
      body:options.raw !== undefined ? options.raw : options.body !== undefined ? JSON.stringify(options.body) : undefined});
    let value;
    try { value = await response.json(); } catch (error) { if (controller.signal.aborted) throw error; throw Error('主机未返回有效响应，请重试'); }
    if (!response.ok) { const error = Error(value.error || '请求失败'); error.status = response.status; throw error; }
    return value;
  } catch (error) {
    if (timedOut) throw Error('连接主机超时，请检查网络后重试');
    throw error;
  } finally { clearTimeout(timer); if (external) external.removeEventListener('abort', cancel); }
}

async function copy(text, label) {
  try { if (!navigator.clipboard) throw Error(); await navigator.clipboard.writeText(text); }
  catch (_) { const field = document.createElement('textarea'); field.value = text; field.style.position = 'fixed'; field.style.opacity = '0'; document.body.appendChild(field); field.select(); const ok = document.execCommand('copy'); field.remove(); if (!ok) return toast('请长按地址或访问码手动复制'); }
  toast(label);
}
function downloadText(text, filename) { const url = URL.createObjectURL(new Blob([text], {type:'text/plain;charset=utf-8'})); const link = document.createElement('a'); link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 30000); }
function setModal(id, open) { $(id).hidden = !open; if (open) { const first = $(id).querySelector('button'); if (first) first.focus(); } }
function currentInvite() { return (selectedUrl || config.lanUrl || location.origin + '/') + (config.requireAccessCode ? '#pin=' + config.accessCode : ''); }

function updateAccessUi() {
  const required = !!config.requireAccessCode;
  $('require-code-toggle').checked = required;
  $('rotate-btn').hidden = !required;
  $('invite-code-area').hidden = !required;
  $('access-mode-note').hidden = required;
  $('invite-hint').textContent = required ? '扫码或输入访问码' : '扫码或打开地址即可加入';
  $('access-code-label').hidden = $('access-code').hidden = !required;
  $('access-code').required = required;
  $('login-description').textContent = required ? '查看主机电脑上的 8 位访问码，加入后即可选择设备互传。' : '无需访问码，加入后即可选择设备互传。';
}

async function updateInvite() {
  updateAccessUi();
  const available = config.networkReady !== false;
  $('copy-url').disabled = !available;
  $('repair-btn').disabled = !available;
  if (!available) { $('lan-url').value = '请先连接 Wi-Fi 或以太网'; $('qr-note').textContent = '连接局域网后显示二维码'; return; }
  $('lan-url').value = selectedUrl || config.lanUrl;
  const code = config.accessCode || '';
  $('invite-code').textContent = config.requireAccessCode ? code.slice(0,4) + ' ' + code.slice(4) : '';
  try { await QRCode.toCanvas($('qr-canvas'), currentInvite(), {width:280, margin:2, errorCorrectionLevel:'M', color:{dark:'#34452b',light:'#ffffff'}}); $('qr-note').textContent = '使用手机系统相机扫码'; }
  catch (_) { $('qr-note').textContent = config.requireAccessCode ? '可复制地址并输入访问码' : '可复制地址到浏览器打开'; }
}
function configureAddresses() {
  const rows = (config.addresses || []).slice().sort((a,b) => Number(a.virtual) - Number(b.virtual));
  if (!rows.some(r => r.url === config.lanUrl)) rows.unshift({url:config.lanUrl, ip:new URL(config.lanUrl).hostname, name:'当前地址'});
  if (!rows.some(r => r.url === selectedUrl)) selectedUrl = config.lanUrl;
  const options = rows.map(r => '<option value="' + esc(r.url) + '">' + esc(r.name + ' · ' + r.ip + (r.virtual ? '（虚拟网卡）' : '')) + '</option>').join('');
  if ($('lan-address').innerHTML !== options) $('lan-address').innerHTML = options;
  $('lan-address').value = selectedUrl;
  updateInvite();
}
async function refreshConfig() {
  const fresh = await api('/api/config');
  const changed = fresh.requireAccessCode !== config.requireAccessCode || fresh.accessCode !== config.accessCode || fresh.networkReady !== config.networkReady || fresh.lanUrl !== config.lanUrl || JSON.stringify(fresh.addresses) !== JSON.stringify(config.addresses);
  config = fresh;
  updateAccessUi();
  if (changed || !selectedUrl) configureAddresses();
  if (config.isHost && config.adminKey) {
    try {
      const status = await api('/api/host/status', {admin:true});
      const labels = {checking:'正在检查网络', allowed:'防火墙规则就绪', missing:'建议检查手机访问', blocked:'发现入站阻止规则', manual:'请检查系统防火墙', unknown:'网络检测暂不可用'};
      $('firewall-status').textContent = labels[status.firewall] || labels.checking;
      $('host-note').textContent = status.message || '正在检查当前网卡与防火墙规则。';
      $('host-count').textContent = ' · ' + status.devices + ' 台设备 · 临时预留 ' + fmt(status.used);
      if (status.repairResult && Date.now() / 1000 - status.repairResult.time < 40) $('host-note').textContent = status.repairResult.message;
    } catch (_) {}
  }
}
function showLogin(error = '') {
  me = null; token = ''; writeStore(localStorage, 'linqdrop-v2-token', '');
  clearTimeout(syncTimer); $('app').hidden = true; $('login-panel').hidden = false; $('leave-btn').hidden = true;
  $('login-name').value = name; $('login-error').textContent = error;
  updateAccessUi();
}
async function connect(pin) {
  name = $('login-name').value.trim() || name;
  const result = await api('/api/connect', {method:'POST', body:{name,kind:phone?'phone':'desktop',pin}, admin:config.isHost});
  token = result.token; writeStore(localStorage, 'linqdrop-v2-token', token); writeStore(localStorage, 'linqdrop-v2-name', name);
  if (location.hash) history.replaceState(null, '', location.pathname + location.search);
  await startApp();
}
async function startApp() {
  const snapshot = await api('/api/sync');
  me = snapshot.me; peers = snapshot.devices; transfers = snapshot.transfers;
  $('login-panel').hidden = true; $('app').hidden = false; $('leave-btn').hidden = false;
  $('self-name').textContent = me.name; $('self-art').textContent = phone ? '▯' : '▣';
  $('host-panel').hidden = !config.isHost;
  if (config.isHost) $('invite-panel').open = true;
  banner(''); await refreshConfig(); renderDevices(); renderTransfers(); presentOffer();
  clearTimeout(syncTimer); syncTimer = setTimeout(sync, 1200);
}
async function sync() {
  if (!me || stopped) return;
  try {
    const snapshot = await api('/api/sync');
    me = snapshot.me; peers = snapshot.devices; transfers = snapshot.transfers;
    $('self-name').textContent = me.name;
    banner(''); renderDevices(); renderTransfers(); presentOffer(); scheduleUploads();
    if (++syncCount % 7 === 0) await refreshConfig();
  } catch (error) {
    if (error.status === 401) { for (const value of localFiles.values()) value.controller && value.controller.abort(); return showLogin('主机服务已重新启动或访问已过期，请重新加入'); }
    banner('暂时连接不到主机，正在重试。请保留页面，并检查 Wi-Fi 和主机服务。');
  }
  if (me && !stopped) syncTimer = setTimeout(sync, document.hidden ? 4000 : 1200);
}

function renderDevices() {
  const online = peers.filter(d => d.online);
  if (selected && !online.some(d => d.id === selected)) selected = '';
  if (!selected && online.length === 1) selected = online[0].id;
  $('device-count').textContent = online.length + ' 台在线';
  const signature = JSON.stringify([online, selected, !!config.requireAccessCode]);
  if (signature === deviceSignature) return;
  deviceSignature = signature;
  $('devices').innerHTML = online.length ? online.map(d => '<button class="device-card ' + (d.id === selected ? 'selected' : '') + '" data-peer="' + esc(d.id) + '"><span class="device-symbol">' + (d.kind === 'phone' ? '▯' : '▣') + '</span><b>' + esc(d.name) + '</b><small>' + esc(d.ip) + '</small><span class="peer-state">在线</span></button>').join('') : '<div class="empty"><span>⌁</span><b>等另一台设备加入</b><p>' + (config.requireAccessCode ? '让对方扫码或输入主机访问码。' : '让对方扫码或打开上方地址即可加入。') + '</p></div>';
  $('receiver').innerHTML = '<option value="">请选择接收设备</option>' + online.map(d => '<option value="' + esc(d.id) + '">' + esc(d.name) + '</option>').join('');
  $('receiver').value = selected;
  $('send-hint').textContent = selected ? '将发送给 ' + online.find(d => d.id === selected).name + '，对方确认后开始。' : '先让另一台设备加入，再选择接收方。';
}
const labels = {offered:'等待接收确认', accepted:'等待上传', uploading:'传输中', paused:'已暂停', ready:'已送达', rejected:'已拒绝', cancelled:'已取消', expired:'文件已过期'};
function renderTransfers() {
  const all = transfers.filter(r => !hidden.has(r.id));
  const rows = all.filter(r => filter === 'all' || (filter === 'send' ? r.sender === me.id : r.receiver === me.id));
  $('transfer-count').textContent = all.length;
  $('transfers').innerHTML = rows.length ? rows.map(r => {
    const sending = r.sender === me.id, local = localFiles.get(r.id);
    const active = ['accepted','uploading','paused'].includes(r.status);
    const percent = r.size ? Math.min(100,Math.floor(r.bytes / r.size * 100)) : r.status === 'ready' ? 100 : 0;
    let status = local && local.error && active ? '可继续传输' : labels[r.status] || r.status;
    if (r.status === 'ready' && !sending) status = r.downloads ? '已发起过下载' : '待下载';
    let buttons = '';
    if (r.status === 'offered' && !sending) buttons += '<button data-action="accept">接收</button><button data-action="reject">拒绝</button>';
    if (sending && active && local) buttons += r.status === 'paused' || local.error ? '<button data-action="resume">继续</button>' : '<button data-action="pause">暂停</button>';
    if (r.status === 'ready' && !sending) buttons += '<button class="download-btn" data-action="download">下载文件</button>';
    if (['offered','accepted','uploading','paused'].includes(r.status)) buttons += '<button data-action="cancel">取消</button>';
    if (r.status === 'ready') buttons += '<button data-action="clear">清理</button>';
    if (['rejected','cancelled','expired'].includes(r.status)) buttons += '<button data-action="hide">隐藏</button>';
    const note = sending && active && !local ? '若发送页已刷新，请取消此任务后重新选择原文件发送。' : local && local.error ? local.error : '';
    return '<article class="transfer-row" data-file="' + esc(r.id) + '"><span class="file-icon">' + (sending ? '↥' : '↧') + '</span><div class="file-content"><div class="file-title">' + esc(r.name) + '</div><div class="file-meta"><span>' + fmt(r.size) + '</span><span>' + (sending ? '发送给 ' + esc(r.receiverName) : '来自 ' + esc(r.senderName)) + '</span><span class="file-status">' + esc(status) + (active ? ' · ' + percent + '%' : '') + '</span>' + (active ? '<span>' + fmt(r.bytes) + ' / ' + fmt(r.size) + '</span>' : '') + '</div>' + (active ? '<div class="progress-track"><span style="width:' + percent + '%"></span></div>' : '') + (note ? '<p class="file-meta error">' + esc(note) + '</p>' : '') + (r.status === 'ready' ? '<p class="hash-note" title="主机收到文件的 SHA-256，可用于下载后人工核验">SHA-256 ' + esc(r.sha256) + '</p>' : '') + '</div><div class="file-actions">' + buttons + '</div></article>';
  }).join('') : '<div class="empty"><span>⇄</span><b>这里，等着你的第一份文件</b><p>选择接收设备后，照片、视频、文档都可以发送。</p></div>';
}
function upsert(value) { const index = transfers.findIndex(r => r.id === value.id); if (index < 0) transfers.unshift(value); else transfers[index] = value; renderTransfers(); }
async function action(id, kind) { const result = await api('/api/transfers/' + id + '/action', {method:'POST',body:{action:kind}}); upsert(result); return result; }
function presentOffer() {
  if (currentOffer && !transfers.some(r => r.id === currentOffer && r.status === 'offered')) { currentOffer = ''; setModal('offer-modal',false); }
  if (currentOffer || !$('help-modal').hidden) return;
  const offer = transfers.find(r => r.receiver === me.id && r.status === 'offered');
  if (!offer) return;
  currentOffer = offer.id; $('offer-name').textContent = offer.name; $('offer-size').textContent = fmt(offer.size); $('offer-peer').textContent = '来自 ' + offer.senderName; $('accept-batch').checked = false; setModal('offer-modal',true);
}
async function answerOffer(accepted) {
  const offer = transfers.find(r => r.id === currentOffer);
  if (!offer) return;
  const list = accepted && $('accept-batch').checked ? transfers.filter(r => r.receiver === me.id && r.sender === offer.sender && r.status === 'offered') : [offer];
  $('accept-btn').disabled = $('reject-btn').disabled = true;
  try { for (const item of list) await action(item.id, accepted ? 'accept' : 'reject'); currentOffer = ''; setModal('offer-modal',false); }
  catch (error) { toast(error.message); }
  finally { $('accept-btn').disabled = $('reject-btn').disabled = false; presentOffer(); }
}

async function selectFiles(files) {
  if (!selected) return toast('请先选择一台在线接收设备');
  const receiver = selected;
  for (const file of files) {
    if (file.size > config.maxFileSize) { toast(file.name + ' 超过单文件 2 GiB 上限'); continue; }
    try {
      const record = await api('/api/transfers', {method:'POST',body:{receiver,name:file.name,size:file.size,requestId:requestId()}});
      localFiles.set(record.id, {file,busy:false,error:'',paused:false}); upsert(record);
    } catch (error) { toast(error.message); }
  }
  toast('已发送文件邀请，等待对方确认');
}
function scheduleUploads() {
  if (!me) return;
  for (const r of transfers.slice().reverse()) {
    if (busyCount >= 2) break;
    const local = localFiles.get(r.id);
    if (r.sender === me.id && ['accepted','uploading'].includes(r.status) && local && !local.busy && !local.error && !local.paused) upload(r,local);
    if (['ready','cancelled','rejected','expired'].includes(r.status) && local && !local.busy) localFiles.delete(r.id);
    if (r.receiver === me.id && r.status === 'ready' && !notified.has(r.id)) { notified.add(r.id); toast(r.name + ' 已接收，可下载到本机'); }
  }
}
async function upload(record, local) {
  local.busy = true; busyCount++; local.controller = new AbortController();
  try {
    let state = await api('/api/transfers/' + record.id);
    upsert(state);
    if (!['accepted','uploading'].includes(state.status)) return;
    for (let offset = state.bytes; offset < local.file.size;) {
      if (local.paused || local.controller.signal.aborted) return;
      const chunk = local.file.slice(offset, Math.min(local.file.size,offset + 1048576));
      const result = await api('/api/transfers/' + record.id + '/chunk?offset=' + offset, {method:'POST',raw:chunk,signal:local.controller.signal,timeout:45000});
      offset = result.bytes; record.bytes = offset; record.status = 'uploading'; upsert(record);
    }
    if (!local.paused && !local.controller.signal.aborted) { await action(record.id,'finish'); toast(record.name + ' 发送完成'); }
  } catch (error) {
    if (!local.paused && !local.controller.signal.aborted) { local.error = '网络中断或主机响应超时。保留此页面，恢复网络后点击“继续”。'; toast(error.message); }
  } finally { local.busy = false; busyCount--; local.controller = null; renderTransfers(); scheduleUploads(); }
}
async function transferAction(id, kind) {
  const record = transfers.find(r => r.id === id); if (!record) return;
  const local = localFiles.get(id);
  if (kind === 'hide') { hidden.add(id); return renderTransfers(); }
  if (kind === 'clear' && !confirm('清理后，对方也无法再下载这份临时文件。请确认已经保存。')) return;
  try {
    if (kind === 'download') {
      const result = await api('/api/transfers/' + id + '/download', {method:'POST',body:{}});
      const link = document.createElement('a'); link.href = result.url; link.download = record.name; document.body.appendChild(link); link.click(); link.remove();
      return toast('已发起下载，请通过浏览器下载列表确认保存');
    }
    if (kind === 'pause' || kind === 'cancel') { if (local) { local.paused = true; if (local.controller) local.controller.abort(); } }
    if (kind === 'resume' && local) {
      if (record.status === 'paused') await action(id,'resume');
      else upsert(await api('/api/transfers/' + id));
      local.error = ''; local.paused = false; scheduleUploads(); return;
    }
    await action(id,kind); presentOffer();
  } catch (error) { toast(error.message); }
}

$('login-name').value = name;
$('login-form').onsubmit = async event => { event.preventDefault(); $('login-btn').disabled = true; $('login-error').textContent = ''; try { await connect(config.requireAccessCode ? $('access-code').value.replace(/\s/g,'') : undefined); } catch (error) { try { config = await api('/api/config'); updateAccessUi(); } catch (_) {} $('login-error').textContent = error.message; } finally { $('login-btn').disabled = false; } };
$('access-code').oninput = event => { const digits = event.target.value.replace(/\D/g,'').slice(0,8); event.target.value = digits.length > 4 ? digits.slice(0,4) + ' ' + digits.slice(4) : digits; };
$('receiver').onchange = () => { selected = $('receiver').value; renderDevices(); };
$('devices').onclick = event => { const button = event.target.closest('[data-peer]'); if (button) { selected = button.dataset.peer; renderDevices(); } };
$('lan-address').onchange = () => { selectedUrl = $('lan-address').value; updateInvite(); };
$('copy-url').onclick = () => copy(selectedUrl || config.lanUrl,'局域网地址已复制');
$('copy-code').onclick = () => copy(config.accessCode,'访问码已复制');
$('choose-btn').onclick = event => { event.stopPropagation(); if (!selected) return toast('请先选择一台接收设备'); $('file-input').click(); };
$('dropzone').onclick = event => { if (event.target.closest('button')) return; if (!selected) return toast('请先选择一台接收设备'); $('file-input').click(); };
$('dropzone').onkeydown = event => { if (event.target === $('dropzone') && ['Enter',' '].includes(event.key)) { event.preventDefault(); $('dropzone').click(); } };
$('file-input').onchange = event => { const files = Array.from(event.target.files); event.target.value = ''; selectFiles(files); };
for (const event of ['dragenter','dragover']) $('dropzone').addEventListener(event,e => { e.preventDefault(); $('dropzone').classList.add('dragging'); });
for (const event of ['dragleave','drop']) $('dropzone').addEventListener(event,e => { e.preventDefault(); $('dropzone').classList.remove('dragging'); if (event === 'drop') selectFiles(Array.from(e.dataTransfer.files)); });
window.addEventListener('dragover',event => event.preventDefault()); window.addEventListener('drop',event => event.preventDefault());
$('transfers').onclick = event => { const button = event.target.closest('[data-action]'); if (button) transferAction(button.closest('[data-file]').dataset.file,button.dataset.action); };
document.querySelectorAll('[data-filter]').forEach(button => button.onclick = () => { filter = button.dataset.filter; document.querySelectorAll('[data-filter]').forEach(b => { b.classList.toggle('active',b === button); b.setAttribute('aria-selected',String(b === button)); }); renderTransfers(); });
$('clear-history').onclick = () => { transfers.filter(r => ['ready','cancelled','rejected','expired'].includes(r.status)).forEach(r => hidden.add(r.id)); renderTransfers(); toast('已隐藏结束记录，临时文件仍按时清理'); };
$('accept-btn').onclick = () => answerOffer(true); $('reject-btn').onclick = () => answerOffer(false);
$('help-btn').onclick = () => setModal('help-modal',true);
for (const id of ['help-close','help-done']) $(id).onclick = () => { setModal('help-modal',false); if (me) presentOffer(); };
$('rename-btn').onclick = async () => { const value = prompt('设备名称',me.name); if (!value || !value.trim()) return; try { await api('/api/device',{method:'POST',body:{name:value.trim()}}); name = value.trim(); me.name = name; $('self-name').textContent = name; writeStore(localStorage,'linqdrop-v2-name',name); } catch (error) { toast(error.message); } };
$('leave-btn').onclick = async () => { if (busyCount && !confirm('当前仍有文件上传，确定退出吗？')) return; for (const local of localFiles.values()) { local.paused = true; if (local.controller) local.controller.abort(); } try { await api('/api/leave',{method:'POST',body:{}}); } catch (_) {} showLogin(); };
$('repair-btn').onclick = async () => { $('repair-btn').disabled = true; try { const result = await api('/api/host/firewall',{method:'POST',admin:true,body:{ip:new URL(selectedUrl || config.lanUrl).hostname},timeout:60000}); toast(result.message); setTimeout(refreshConfig,4000); } catch (error) { toast(error.message); } finally { $('repair-btn').disabled = false; } };
$('report-btn').onclick = async () => { try { const result = await api('/api/host/report',{method:'POST',admin:true,body:{}}); downloadText(result.text,'LinqDrop-网络诊断.txt'); } catch (error) { toast(error.message); } };
$('require-code-toggle').onchange = async () => {
  const toggle = $('require-code-toggle');
  toggle.disabled = true;
  try {
    const result = await api('/api/host/access-code',{method:'POST',admin:true,body:{required:toggle.checked}});
    config.requireAccessCode = result.requireAccessCode; config.accessCode = result.accessCode;
    updateInvite(); renderDevices();
    toast(result.requireAccessCode ? '已开启访问码，已加入的设备可继续使用' : '已关闭访问码，打开地址即可加入');
  } catch (error) { toggle.checked = !!config.requireAccessCode; toast(error.message); }
  finally { toggle.disabled = false; }
};
$('rotate-btn').onclick = async () => { try { const result = await api('/api/host/rotate-code',{method:'POST',admin:true,body:{}}); config.accessCode = result.accessCode; updateInvite(); toast('访问码已更换，已加入的设备可继续使用'); } catch (error) { toast(error.message); } };
$('stop-btn').onclick = async () => { if (!confirm('停止服务将断开所有设备，并清理尚未保存的临时文件。确定停止吗？')) return; try { await api('/api/host/shutdown',{method:'POST',admin:true,body:{}}); stopped = true; clearTimeout(syncTimer); banner('服务已停止。再次双击桌面“邻传 LinqDrop”即可启动。'); } catch (error) { toast(error.message); } };
window.addEventListener('beforeunload',event => { if (busyCount || transfers.some(r => r.sender === (me && me.id) && ['offered','accepted','uploading','paused'].includes(r.status))) { event.preventDefault(); event.returnValue = ''; } });
document.addEventListener('visibilitychange',() => { if (!document.hidden && me && !stopped) { clearTimeout(syncTimer); syncTimer = setTimeout(sync,100); } });

async function init() {
  try {
    config = await api('/api/config');
    updateAccessUi();
    const match = location.hash.match(/pin=(\d{8})/);
    if (token) { try { await startApp(); return; } catch (error) { if (error.status !== 401) throw error; token = ''; writeStore(localStorage,'linqdrop-v2-token',''); } }
    if (config.isHost || !config.requireAccessCode) { await connect(config.accessCode); return; }
    showLogin();
    if (match) { $('access-code').value = match[1]; await connect(match[1]); }
  } catch (error) { try { config = await api('/api/config'); } catch (_) {} showLogin('无法连接主机：' + error.message); }
}
init();
