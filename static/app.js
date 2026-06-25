const form = document.querySelector('#job-form');
const files = document.querySelector('#files');
const references = document.querySelector('#references');
const panel = document.querySelector('#progress-panel');
const button = document.querySelector('#start');
const cancelButton = document.querySelector('#cancel-job');
const stageList = document.querySelector('#stage-list');
const jobLog = document.querySelector('#job-log');
let currentJobId = null;
let renderedDecisionKey = '';
let pollFailures = 0;
let pollInFlight = false;
let pollTimer = null;
let lastPollSuccess = Date.now();

const helpText = {
  'care-stage': ['原生生成阶段', 'Stage I 先根据线稿、参考图和颜色提示产生固有色；Stage II 再以 Stage I 的平涂结果作为条件进行精细渲染。成品通常选择 Stage II，后期绘画底稿可选择 Stage I。'],
  finish: ['原生输出类型', 'Flat 是封闭区域的纯色层；Smoothed 加入连续色彩与细节；Blended 会重新融合原始线稿。Style2Paints 会同时生成这些结果，勾选保存全部层后可统一下载。']
};

files.addEventListener('change', () => {
  document.querySelector('#file-state').textContent = files.files.length ? `已选择 ${files.files.length} 个文件` : '选择文件';
});
references.addEventListener('change', () => {
  const count = references.files.length;
  document.querySelector('#ref-state').innerHTML = count ? `<strong>✓ 已选择 ${count} 张样例</strong><small>会逐页自动匹配最接近的参考图</small>` : '<strong>＋ 添加多张彩色样例</strong><small>人物、服装、场景越完整越容易匹配</small>';
});
for (const [name, id] of [['reference_strength','ref-out'],['saturation','sat-out'],['strength','str-out'],['line_protection','line-out'],['lineart_strength','lineart-strength-out'],['lineart_detail','lineart-detail-out'],['lineart_weight','lineart-weight-out']]) {
  const input = form.elements[name];
  if (!input) continue;
  input.addEventListener('input', () => document.querySelector(`#${id}`).textContent = `${Math.round(input.value * 100)}%`);
}
for (const [name, id, suffix] of [
  ['cobra_steps','cobra-steps-out',''],
  ['cobra_top_k','cobra-topk-out',''],
  ['cobra_color_strength','cobra-color-out','%'],
  ['cobra_preserve_lines','cobra-lines-out','%'],
  ['cobra_consistency_strength','cobra-consistency-out','%']
]) {
  const input = form.elements[name];
  if (!input) continue;
  input.addEventListener('input', () => {
    const value = suffix === '%' ? Math.round(input.value * 100) : input.value;
    document.querySelector(`#${id}`).textContent = `${value}${suffix}`;
  });
}

for (const help of document.querySelectorAll('.help')) {
  help.addEventListener('click', () => {
    const [title, body] = helpText[help.dataset.help];
    document.querySelector('#help-title').textContent = title;
    document.querySelector('#help-body').textContent = body;
    document.querySelector('#help-dialog').showModal();
  });
}
document.querySelector('.dialog-close').addEventListener('click', () => document.querySelector('#help-dialog').close());
document.querySelector('#log-toggle').addEventListener('click', (event) => {
  jobLog.classList.toggle('hidden');
  event.currentTarget.textContent = jobLog.classList.contains('hidden') ? '展开' : '收起';
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  button.disabled = true;
  button.textContent = '正在上传…';
  panel.classList.remove('hidden');
  panel.scrollIntoView({behavior:'smooth'});
  stageList.innerHTML = '';
  jobLog.innerHTML = '';
  document.querySelector('#previews').innerHTML = '';
  document.querySelector('#downloads').innerHTML = '';
  cancelButton.disabled = true;
  try {
    const response = await fetch('/api/jobs', {method:'POST', body:new FormData(form)});
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.error || '创建任务失败');
    currentJobId = data.job_id;
    history.replaceState(null, '', `/?job=${encodeURIComponent(currentJobId)}`);
    cancelButton.disabled = false;
    poll(data.job_id);
  } catch (error) {
    showError(error.message);
  }
});

cancelButton.addEventListener('click', async () => {
  if (!currentJobId) return;
  cancelButton.disabled = true;
  document.querySelector('#progress-message').textContent = '正在取消任务…';
  try {
    await fetch(`/api/jobs/${currentJobId}/cancel`, {method:'POST'});
  } catch (_error) {}
  button.disabled = false;
  button.textContent = '重新提交';
});

function renderStages(stages) {
  stageList.innerHTML = stages.map(stage => `
    <div class="stage ${stage.status}">
      <div><strong>${stage.label}</strong><span>${stage.message}</span><b>${stage.progress}%</b></div>
      <i><em style="width:${stage.progress}%"></em></i>
    </div>`).join('');
}

function renderLogs(logs) {
  if (jobLog.dataset.count === String(logs.length)) return;
  jobLog.innerHTML = logs.map(item => `<div class="${item.level}"><time>${item.time}</time><span>${escapeHtml(item.message)}</span></div>`).join('');
  jobLog.dataset.count = String(logs.length);
  jobLog.scrollTop = jobLog.scrollHeight;
}

function renderDecision(decision) {
  const box = document.querySelector('#decision-panel');
  if (!decision) {
    box.classList.add('hidden');
    renderedDecisionKey = '';
    return;
  }
  const key = `${decision.kind}:${decision.page}`;
  box.classList.remove('hidden');
  if (renderedDecisionKey === key) return;
  renderedDecisionKey = key;
  document.querySelector('#decision-title').textContent = decision.title;
  document.querySelector('#decision-message').textContent = decision.message;
  const source = document.querySelector('#decision-source');
  source.src = `${decision.source}?v=${Date.now()}`;
  document.querySelector('#decision-source-link').href = decision.source;
  document.querySelector('#decision-options').innerHTML = decision.options.map(option => `
    <label class="decision-option">
      <input type="radio" name="reference-decision" value="${option.id}">
      <span><img src="${option.image}" alt="${escapeHtml(option.label)}"><b>${escapeHtml(option.label)}</b></span>
      ${option.recommended ? '<em>推荐</em>' : ''}
    </label>`).join('');
  const confirm = document.querySelector('#decision-confirm');
  confirm.disabled = true;
  document.querySelectorAll('input[name="reference-decision"]').forEach(input => {
    input.addEventListener('change', () => confirm.disabled = false);
  });
}

document.querySelector('#decision-confirm').addEventListener('click', async () => {
  const selected = document.querySelector('input[name="reference-decision"]:checked');
  if (!selected || !currentJobId) return;
  const data = new FormData();
  data.append('choice', selected.value);
  document.querySelector('#decision-confirm').disabled = true;
  await fetch(`/api/jobs/${currentJobId}/decision`, {method:'POST', body:data});
});

document.querySelector('#decision-reference').addEventListener('change', async (event) => {
  const reference = event.target.files[0];
  if (!reference || !currentJobId) return;
  const data = new FormData();
  data.append('reference', reference);
  event.target.disabled = true;
  await fetch(`/api/jobs/${currentJobId}/decision`, {method:'POST', body:data});
  event.target.disabled = false;
  event.target.value = '';
});

function schedulePoll(id, delay = 700) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => poll(id), delay);
}

async function poll(id) {
  if (pollInFlight) return;
  pollInFlight = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(`/api/jobs/${id}`, {
      signal: controller.signal,
      cache: 'no-store'
    });
    const job = await readJson(response);
    if (!response.ok) throw new Error(job.error || `接口返回 ${response.status}`);
    pollFailures = 0;
    lastPollSuccess = Date.now();
    const percent = job.overall_progress || 0;
    document.querySelector('#progress-message').textContent = job.message;
    document.querySelector('#progress-number').textContent = `${percent}%`;
    document.querySelector('#progress-bar').style.width = `${percent}%`;
    renderStages(job.stages || []);
    renderLogs(job.logs || []);
    renderDecision(job.decision);
    const previews = document.querySelector('#previews');
    if (previews.children.length !== job.previews.length) {
      previews.innerHTML = job.previews.slice(-6).map(url => `<a href="${url}" target="_blank" rel="noopener"><img src="${url}?v=${job.progress}" alt="高清预览"></a>`).join('');
    }
    if (job.status === 'done') {
      document.querySelector('#progress-number').textContent = '完成';
      document.querySelector('#progress-bar').style.width = '100%';
      const links = [];
      if (job.downloads.pdf) links.push(`<a href="${job.downloads.pdf}">下载彩色 PDF</a>`);
      if (job.downloads.cbz) links.push(`<a href="${job.downloads.cbz}">下载彩色 CBZ</a>`);
      if (job.downloads.bundle) links.push(`<a href="${job.downloads.bundle}">下载分卷 PDF / CBZ 合集</a>`);
      if (job.downloads.lineart) links.push(`<a href="${job.downloads.lineart}">下载高清重绘线稿 ZIP</a>`);
      if (job.downloads.layers) links.push(`<a href="${job.downloads.layers}">下载 8 层 Style2Paints ZIP</a>`);
      document.querySelector('#downloads').innerHTML = links.join('');
      cancelButton.disabled = true;
      button.disabled = false;
      button.innerHTML = '再处理一部 <span>→</span>';
      return;
    }
    if (job.status === 'cancelled') {
      document.querySelector('#progress-number').textContent = '已取消';
      cancelButton.disabled = true;
      button.disabled = false;
      button.textContent = '重新提交';
      return;
    }
    if (job.status === 'error') throw new Error(job.error || '处理失败');
    schedulePoll(id);
  } catch (error) {
    // A large PDF render can briefly delay Flask's response. The backend job
    // continues independently, so keep reconnecting instead of presenting a
    // false task failure and abandoning status polling.
    if (
      error.name === 'AbortError'
      || error instanceof TypeError
      || /Failed to fetch|NetworkError|Load failed/i.test(error.message)
    ) {
      pollFailures += 1;
      document.querySelector('#progress-message').textContent =
        `本地服务暂时繁忙，正在自动重连（第 ${pollFailures} 次）`;
      document.querySelector('#progress-number').textContent = '重连中';
      const delay = Math.min(5000, 900 + pollFailures * 350);
      schedulePoll(id, delay);
      return;
    }
    showError(error.message);
  } finally {
    clearTimeout(timeout);
    pollInFlight = false;
  }
}

async function readJson(response) {
  const text = await response.text();
  try {
    return text ? JSON.parse(text) : {};
  } catch (_error) {
    const clean = text.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
    return {
      error: clean
        ? `服务器返回了非 JSON 内容：${clean.slice(0, 180)}`
        : '服务器返回了空响应，请重新提交一次。'
    };
  }
}

function escapeHtml(value) {
  const node = document.createElement('span');
  node.textContent = value;
  return node.innerHTML;
}

function showError(message) {
  document.querySelector('#progress-message').textContent = message;
  document.querySelector('#progress-number').textContent = '失败';
  jobLog.innerHTML += `<div class="error"><time>错误</time><span>${escapeHtml(message)}</span></div>`;
  button.disabled = false;
  button.textContent = '重试';
}

const resumedJobId = new URLSearchParams(location.search).get('job');
if (resumedJobId) {
  currentJobId = resumedJobId;
  panel.classList.remove('hidden');
  cancelButton.disabled = false;
  poll(resumedJobId);
}

setInterval(() => {
  if (
    currentJobId
    && !pollInFlight
    && Date.now() - lastPollSuccess > 15000
  ) {
    schedulePoll(currentJobId, 0);
  }
}, 5000);
