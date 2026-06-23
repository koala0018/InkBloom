const form = document.querySelector('#job-form');
const files = document.querySelector('#files');
const references = document.querySelector('#references');
const panel = document.querySelector('#progress-panel');
const button = document.querySelector('#start');
const stageList = document.querySelector('#stage-list');
const jobLog = document.querySelector('#job-log');

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
for (const [name, id] of [['reference_strength','ref-out'],['saturation','sat-out'],['strength','str-out'],['line_protection','line-out']]) {
  const input = form.elements[name];
  input.addEventListener('input', () => document.querySelector(`#${id}`).textContent = `${Math.round(input.value * 100)}%`);
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
  try {
    const response = await fetch('/api/jobs', {method:'POST', body:new FormData(form)});
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.error || '创建任务失败');
    poll(data.job_id);
  } catch (error) {
    showError(error.message);
  }
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

async function poll(id) {
  try {
    const response = await fetch(`/api/jobs/${id}`);
    const job = await readJson(response);
    if (!response.ok) throw new Error(job.error || `接口返回 ${response.status}`);
    const percent = job.overall_progress || 0;
    document.querySelector('#progress-message').textContent = job.message;
    document.querySelector('#progress-number').textContent = `${percent}%`;
    document.querySelector('#progress-bar').style.width = `${percent}%`;
    renderStages(job.stages || []);
    renderLogs(job.logs || []);
    const previews = document.querySelector('#previews');
    if (previews.children.length !== job.previews.length) {
      previews.innerHTML = job.previews.slice(-6).map(url => `<img src="${url}?v=${job.progress}" alt="上色预览">`).join('');
    }
    if (job.status === 'done') {
      document.querySelector('#progress-number').textContent = '完成';
      document.querySelector('#progress-bar').style.width = '100%';
      const links = [];
      if (job.downloads.pdf) links.push(`<a href="${job.downloads.pdf}">下载彩色 PDF</a>`);
      if (job.downloads.cbz) links.push(`<a href="${job.downloads.cbz}">下载彩色 CBZ</a>`);
      if (job.downloads.layers) links.push(`<a href="${job.downloads.layers}">下载 8 层 Style2Paints ZIP</a>`);
      document.querySelector('#downloads').innerHTML = links.join('');
      button.disabled = false;
      button.innerHTML = '再处理一部 <span>→</span>';
      return;
    }
    if (job.status === 'error') throw new Error(job.error || '处理失败');
    setTimeout(() => poll(id), 700);
  } catch (error) { showError(error.message); }
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
