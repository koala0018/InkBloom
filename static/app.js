const form = document.querySelector('#job-form');
const files = document.querySelector('#files');
const references = document.querySelector('#references');
const panel = document.querySelector('#progress-panel');
const button = document.querySelector('#start');

files.addEventListener('change', () => {
  document.querySelector('#file-state').textContent = files.files.length ? `已选 ${files.files.length} 个文件` : '选择文件';
});
references.addEventListener('change', () => {
  const count = references.files.length;
  document.querySelector('#ref-state').innerHTML = count ? `<strong>✓ 已选择 ${count} 张样例</strong><small>将按页面和区域自动匹配</small>` : '<strong>＋ 添加多张彩色样例</strong><small>人物、服装、场景越完整越准确</small>';
});
for (const [name, id] of [['reference_strength','ref-out'],['saturation','sat-out'],['strength','str-out'],['line_protection','line-out']]) {
  const input = form.elements[name];
  input.addEventListener('input', () => document.querySelector(`#${id}`).textContent = `${Math.round(input.value * 100)}%`);
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  button.disabled = true;
  button.textContent = '正在上传…';
  panel.classList.remove('hidden');
  panel.scrollIntoView({behavior:'smooth'});
  try {
    const response = await fetch('/api/jobs', {method:'POST', body:new FormData(form)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '创建任务失败');
    poll(data.job_id);
  } catch (error) {
    showError(error.message);
  }
});

async function poll(id) {
  try {
    const response = await fetch(`/api/jobs/${id}`);
    const job = await response.json();
    const percent = job.total ? Math.round(job.progress / job.total * 100) : 2;
    document.querySelector('#progress-message').textContent = job.message;
    document.querySelector('#progress-number').textContent = `${percent}%`;
    document.querySelector('#progress-bar').style.width = `${percent}%`;
    const previews = document.querySelector('#previews');
    if (previews.children.length !== job.previews.length) {
      previews.innerHTML = job.previews.slice(-6).map((url, i) => `<img src="${url}?v=${job.progress}" alt="上色预览">`).join('');
    }
    if (job.status === 'done') {
      document.querySelector('#progress-number').textContent = '完成';
      document.querySelector('#downloads').innerHTML = `<a href="${job.downloads.pdf}">下载彩色 PDF</a><a href="${job.downloads.cbz}">下载彩色 CBZ</a>`;
      button.disabled = false; button.innerHTML = '再处理一部 <span>→</span>'; return;
    }
    if (job.status === 'error') throw new Error(job.error || '处理失败');
    setTimeout(() => poll(id), 700);
  } catch (error) { showError(error.message); }
}

function showError(message) {
  document.querySelector('#progress-message').textContent = message;
  document.querySelector('#progress-number').textContent = '失败';
  button.disabled = false; button.textContent = '重试';
}
