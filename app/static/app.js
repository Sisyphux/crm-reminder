// ========== 客户跟进提醒系统 - 应用逻辑 ==========
// ========== Utility ==========
function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/[&<>"']/g, function(m) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":"&#39;"}[m];
  });
}

// ========== Global State ==========
let currentPage = 'dashboard';
let calendarData = {};
let calendarYear, calendarMonth;
let selectedCustomers = new Set();
let selectedNewPool = new Set();

// ========== Initialization ==========
document.addEventListener('DOMContentLoaded', function() {
  const now = new Date();
  calendarYear = now.getFullYear();
  calendarMonth = now.getMonth();
  document.getElementById('dashDate').textContent = now.toLocaleDateString('en-US', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
  loadDashboard();
});

// ========== Toast ==========
function showToast(message, type) {
  type = type || 'info';
  var container = document.getElementById('toastContainer');
  var toast = document.createElement('div');
  toast.className = 'toast ' + type;
  var icons = { success: '&#10003;', error: '&#10007;', info: '&#8505;', warning: '&#9888;' };
  toast.innerHTML = '<span>' + (icons[type] || '') + '</span><span>' + message + '</span>';
  container.appendChild(toast);
  setTimeout(function() { toast.style.opacity = '0'; toast.style.transform = 'translateX(20px)'; setTimeout(function() { toast.remove(); }, 300); }, 3500);
}

// ========== Page Navigation ==========
function switchPage(page) {
  currentPage = page;
  document.querySelectorAll('.page-section').forEach(function(s) { s.classList.remove('active'); });
  document.getElementById('page-' + page).classList.add('active');
  document.querySelectorAll('.nav-item').forEach(function(n) { n.classList.remove('active'); });
  document.querySelector('.nav-item[data-page="' + page + '"]').classList.add('active');
  document.getElementById('sidebar').classList.remove('open');
  switch(page) {
    case 'dashboard': loadDashboard(); break;
    case 'customers': loadCustomers(); break;
    case 'newpool': loadNewPool(); break;
    case 'calendar': loadCalendar(); initIcalUrl(); break;
    case 'history': loadHistory(); break;
    case 'logs': loadLogs('all'); break;
    case 'settings': loadSettings(); break;
  }
}

function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
}

// ========== API Helper ==========
async function api(url, options) {
  options = options || {};
  try {
    var resp = await fetch(url, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options));
    if (!resp.ok) {
      var err = await resp.json().catch(function() { return {}; });
      throw new Error(err.error || 'Request failed');
    }
    return await resp.json();
  } catch (e) {
    showToast('网络错误: ' + e.message, 'error');
    throw e;
  }
}

// ========== Badge Helpers ==========
function levelBadge(level) {
  var cls = { 'A': 'badge-level-a', 'B': 'badge-level-b', 'C': 'badge-level-c', 'C+': 'badge-level-cp', 'D': 'badge-level-d' };
  return '<span class="badge ' + (cls[level] || 'badge-level-c') + '">' + (level || '-') + '</span>';
}
function statusBadge(status) {
  var map = {
    '未建联': 'badge-status-pending', '已建联': 'badge-status-active',
    '跟进中': 'badge-status-following', '成交': 'badge-status-done', '流失': 'badge-status-lost',
    'Following': 'badge-status-following', 'Closed': 'badge-status-done', 'Lost': 'badge-status-lost',
    'pending': 'badge-status-pending', 'replied': 'badge-status-active',
    'bounced': 'badge-status-lost', 'no_reply': 'badge-status-following'
  };
  return '<span class="badge ' + (map[status] || 'badge-status-pending') + '">' + (status || '-') + '</span>';
}
function formatDate(d) { return d ? d.substring(0, 10) : '-'; }
function isOverdue(d) { return d ? d < new Date().toISOString().split('T')[0] : false; }

// ========== DASHBOARD ==========
async function loadDashboard() {
  try {
    var stats = await api('/api/stats');
    document.getElementById('statPending').textContent = stats.pending || 0;
    document.getElementById('statOverdue').textContent = stats.overdue || 0;
    document.getElementById('statFollowing').textContent = stats.following || 0;


    // Status distribution
    var distEl = document.getElementById('statusDist');
    var total = stats.total || 1;
    var statusColors = { '未建联': '#B8860B', '已建联': '#5B7B5A', '跟进中': '#5F7B8B', '成交': '#8B6F4E', '流失': '#A0522D' };
    var sc = stats.status_counts || {};
    var distHtml = '';
    for (var s in sc) {
      var c = sc[s];
      var pct = Math.round(c / total * 100);
      distHtml += '<div style="margin-bottom:12px;"><div style="display:flex;justify-content:space-between;font-size:0.82rem;"><span style="color:var(--fg-secondary);">' + s + '</span><span style="color:var(--fg-muted);">' + c + ' (' + pct + '%)</span></div><div class="progress-bar"><div class="progress-fill" style="width:' + pct + '%;background:' + (statusColors[s] || '#9B8E82') + ';"></div></div></div>';
    }
    if (!distHtml) distHtml = '<div class="empty-state"><p>暂无数据</p></div>';
    distEl.innerHTML = distHtml;

    // Today's reminders
    var reminders = await api('/api/reminders/today');
    var remEl = document.getElementById('todayReminders');
    if (reminders.length === 0) {
      remEl.innerHTML = '<div class="empty-state"><div class="empty-icon">&#9998;</div><p>今日暂无待办任务</p></div>';
    } else {
      var html = '';
      reminders.forEach(function(r) {
        var overdue = r.remind_date < new Date().toISOString().split('T')[0];
        // Customer basic info row - name clickable to open detail, website clickable
        var basicInfo = '<a href="javascript:void(0)" onclick="openEditModal(' + r.customer_id + ')" style="color:var(--brand-600);font-size:0.85rem;font-weight:600;text-decoration:none;">' +
          escapeHtml(r.customer_company || r.customer_name || 'Unknown') + '</a>';
        if (r.country) basicInfo += ' <span style="color:var(--fg-light);margin:0 4px;">·</span> <span style="color:var(--fg-secondary);font-size:0.8rem;">' + escapeHtml(r.country) + '</span>';
        if (r.customer_type === '中间商' || r.customer_type === '终端') basicInfo += ' <span class="badge" style="background:var(--bg-warm);color:var(--fg-muted);border-color:var(--border);padding:1px 6px;font-size:0.68rem;">' + escapeHtml(r.customer_type) + '</span>';
        if (r.field) basicInfo += ' <span style="color:var(--fg-light);margin:0 4px;">·</span> <span style="color:var(--fg-muted);font-size:0.75rem;">' + escapeHtml(r.field) + '</span>';
        if (r.website) { var ws = r.website; if (!/^https?:\/\//i.test(ws)) ws = 'http://' + ws; basicInfo += ' <span style="color:var(--fg-light);margin:0 4px;">·</span> <a href="' + escapeHtml(ws) + '" target="_blank" rel="noopener" style="color:var(--accent);font-size:0.75rem;text-decoration:none;" title="打开客户网站">&#x1F517; 网站</a>'; }
        // Profile
        var profileHtml = r.profile ? '<div style="font-size:0.78rem;color:var(--fg-muted);margin-top:4px;line-height:1.5;">' + escapeHtml(r.profile) + '</div>' : '';
        // Last contact
        var lastContactHtml = r.last_contact ? '<div style="font-size:0.75rem;color:var(--fg-light);margin-top:4px;">&#128337; 上次联系: <span style="color:var(--fg-muted);">' + formatDate(r.last_contact) + '</span></div>' : '';
        // Reminder task
        var taskHtml = '<div style="font-size:0.8rem;color:var(--fg-secondary);margin-top:8px;padding:8px 10px;background:var(--bg);border-radius:var(--radius-xs);border-left:3px solid ' + (overdue ? 'var(--danger)' : 'var(--accent)') + ';">' + escapeHtml(r.content || '') + '</div>';
        // Action button
        html += '<div class="reminder-item">' +
          '<div class="reminder-info">' +
            '<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">' + basicInfo + '</div>' +
            profileHtml +
            lastContactHtml +
            taskHtml +
          '</div>' +
          '<div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px;flex-shrink:0;">' +
            (overdue ? '<span class="badge badge-overdue">逾期</span>' : '<span class="badge badge-today">今日</span>') +
            (r.level ? levelBadge(r.level) : '') +
            '<button class="btn btn-sm" onclick="openCompleteModal(' + r.id + ')">记录跟进</button>' +
          '</div>' +
        '</div>';
      });
      remEl.innerHTML = html;
    }

    // Recent activity
    var logs = await api('/api/logs?limit=8');
    var actEl = document.getElementById('recentActivity');
    if (logs.length === 0) {
      actEl.innerHTML = '<div class="empty-state"><p>暂无最近动态</p></div>';
    } else {
      var html = '';
      logs.forEach(function(l) {
        html += '<div class="log-item"><span class="log-time">' + (l.created_at || '') + '</span><span class="log-action">' + (l.action || '') + '</span><span class="log-detail">' + (l.details || '') + '</span></div>';
      });
      actEl.innerHTML = html;
    }
  } catch(e) { console.error(e); }
}

async function batchCompleteToday() {
  try {
    var reminders = await api('/api/reminders/today');
    if (reminders.length === 0) { showToast('今日暂无任务', 'info'); return; }
    var ids = reminders.map(function(r) { return r.id; });
    await api('/api/reminders/batch/complete', { method: 'POST', body: JSON.stringify({ ids: ids }) });
    showToast('全部任务已完成', 'success');
    loadDashboard();
  } catch(e) {}
}

// ========== CUSTOMERS LIST ==========
async function loadCustomers() {
  var search = document.getElementById('customerSearch').value;
  var level = document.getElementById('customerLevelFilter').value;
  var status = document.getElementById('customerStatusFilter').value;
  try {
    var params = new URLSearchParams({ customer_type: 'existing', search: search, level: level, status: status });
    var data = await api('/api/customers?' + params.toString());
    renderCustomerTable('customerTableBody', data.customers, 'existing');
  } catch(e) {}
}

function renderCustomerTable(tbodyId, customers, type) {
  var tbody = document.getElementById(tbodyId);
  if (!customers || customers.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8"><div class="empty-state"><div class="empty-icon">&#9830;</div><p>No clients found</p></div></td></tr>';
    return;
  }
  var html = '';
  customers.forEach(function(c) {
    var selId = type === 'existing' ? 'custSel_' + c.id : 'newSel_' + c.id;
    html += '<tr>' +
      '<td><input type="checkbox" class="table-checkbox" id="' + selId + '" data-id="' + c.id + '" data-type="' + type + '" onchange="updateSelection(\'' + type + '\')"></td>' +
      '<td><strong style="color:var(--brand-600);font-weight:600;">' + (c.name || '-') + '</strong></td>' +
      '<td style="color:var(--fg-secondary);">' + (c.company || '-') + '</td><td style="color:var(--fg-secondary);">' + (c.country || '-') + '</td>' +
      '<td>' + levelBadge(c.level) + '</td><td>' + statusBadge(c.status) + '</td>' +
      '<td>' + (c.next_follow_up ? '<span style="color:' + (isOverdue(c.next_follow_up) ? 'var(--danger)' : 'var(--fg-secondary)') + '">' + formatDate(c.next_follow_up) + '</span>' : '-') + '</td>' +
      '<td><button class="btn-icon" title="Edit" onclick="openEditModal(' + c.id + ')">&#9998;</button><button class="btn-icon" title="Delete" onclick="deleteCustomer(' + c.id + ')" style="margin-left:4px;">&#10007;</button></td></tr>';
  });
  tbody.innerHTML = html;
}

// Selection management
function updateSelection(type) {
  if (type === 'existing') {
    selectedCustomers.clear();
    document.querySelectorAll('#customerTableBody input[type=checkbox]:checked').forEach(function(cb) { selectedCustomers.add(parseInt(cb.dataset.id)); });
    var bar = document.getElementById('customerBatchBar');
    if (selectedCustomers.size > 0) { bar.classList.add('show'); document.getElementById('customerBatchCount').textContent = selectedCustomers.size + ' selected'; }
    else { bar.classList.remove('show'); }
  } else {
    selectedNewPool.clear();
    document.querySelectorAll('#newPoolTableBody input[type=checkbox]:checked').forEach(function(cb) { selectedNewPool.add(parseInt(cb.dataset.id)); });
    var bar = document.getElementById('newPoolBatchBar');
    if (selectedNewPool.size > 0) { bar.classList.add('show'); document.getElementById('newPoolBatchCount').textContent = selectedNewPool.size + ' selected'; }
    else { bar.classList.remove('show'); }
  }
}
function toggleAllCustomers(cb) { document.querySelectorAll('#customerTableBody input[type=checkbox]').forEach(function(c) { c.checked = cb.checked; }); updateSelection('existing'); }
function toggleAllNewPool(cb) { document.querySelectorAll('#newPoolTableBody input[type=checkbox]').forEach(function(c) { c.checked = cb.checked; }); updateSelection('new'); }
function clearCustomerSelection() { selectedCustomers.clear(); document.getElementById('customerSelectAll').checked = false; document.querySelectorAll('#customerTableBody input[type=checkbox]').forEach(function(c) { c.checked = false; }); document.getElementById('customerBatchBar').classList.remove('show'); }
function clearNewPoolSelection() { selectedNewPool.clear(); document.getElementById('newPoolSelectAll').checked = false; document.querySelectorAll('#newPoolTableBody input[type=checkbox]').forEach(function(c) { c.checked = false; }); document.getElementById('newPoolBatchBar').classList.remove('show'); }

// Batch operations
function batchSetLevel() { openBatchSetModal('level', 'existing'); }
function batchSetStatus() { openBatchSetModal('status', 'existing'); }
function batchSetLevelNew() { openBatchSetModal('level', 'new'); }
function batchSetStatusNew() { openBatchSetModal('status', 'new'); }

function openBatchSetModal(field, type) {
  document.getElementById('batchSetField').value = field;
  document.getElementById('batchSetType').value = type;
  var sel = document.getElementById('batchSetValue');
  sel.innerHTML = '';
  if (field === 'level') {
    document.getElementById('batchSetTitle').textContent = '设置等级';
    document.getElementById('batchSetLabel').textContent = '等级';
    ['A','B','C','C+','D'].forEach(function(v) { sel.innerHTML += '<option value="' + v + '">' + v + '</option>'; });
  } else {
    document.getElementById('batchSetTitle').textContent = '设置状态';
    document.getElementById('batchSetLabel').textContent = '状态';
    ['未建联','已建联','跟进中','成交','流失'].forEach(function(v) { sel.innerHTML += '<option value="' + v + '">' + v + '</option>'; });
  }
  openModal('batchSetModal');
}

async function submitBatchSet() {
  var field = document.getElementById('batchSetField').value;
  var type = document.getElementById('batchSetType').value;
  var value = document.getElementById('batchSetValue').value;
  var ids = type === 'existing' ? Array.from(selectedCustomers) : Array.from(selectedNewPool);
  if (ids.length === 0) { showToast('请选择要操作的项', 'warning'); return; }
  try {
    var endpoint = field === 'level' ? '/api/customers/batch/level' : '/api/customers/batch/status';
    await api(endpoint, { method: 'POST', body: JSON.stringify({ ids: ids, value: value }) });
    showToast('批量更新成功', 'success');
    closeModal('batchSetModal');
    if (type === 'existing') { clearCustomerSelection(); loadCustomers(); }
    else { clearNewPoolSelection(); loadNewPool(); }
  } catch(e) {}
}

async function batchDeleteCustomers() {
  if (!confirm('确认删除 ' + selectedCustomers.size + ' 个选中客户?')) return;
  try {
    await api('/api/customers/batch/delete', { method: 'POST', body: JSON.stringify({ ids: Array.from(selectedCustomers) }) });
    showToast('批量删除成功', 'success');
    clearCustomerSelection(); loadCustomers();
  } catch(e) {}
}
async function batchDeleteNew() {
  if (!confirm('Delete ' + selectedNewPool.size + ' selected clients?')) return;
  try {
    await api('/api/customers/batch/delete', { method: 'POST', body: JSON.stringify({ ids: Array.from(selectedNewPool) }) });
    showToast('批量删除成功', 'success');
    clearNewPoolSelection(); loadNewPool();
  } catch(e) {}
}

// ========== NEW CLIENT POOL ==========
async function loadNewPool() {
  var search = document.getElementById('newPoolSearch').value;
  var level = document.getElementById('newPoolLevelFilter').value;
  var status = document.getElementById('newPoolStatusFilter').value;
  try {
    var params = new URLSearchParams({ customer_type: 'new', search: search, level: level, status: status });
    var data = await api('/api/customers?' + params.toString());
    renderCustomerTable('newPoolTableBody', data.customers, 'new');
  } catch(e) {}
}

// ========== CUSTOMER EDIT MODAL ==========
async function openEditModal(id) {
  try {
    var c = await api('/api/customers/' + id);
    document.getElementById('editCustomerId').value = c.id;
    document.getElementById('customerEditTitle').textContent = '编辑 - ' + c.name;
    document.getElementById('editName').value = c.name || '';
    document.getElementById('editCompany').value = c.company || '';
    setSelectedCountries('editCountryContainer', c.country || '');
    document.getElementById('editCountry').value = c.country || '';
    document.getElementById('editLevel').value = c.level || 'C';
    document.getElementById('editType').value = c.type || '';
    document.getElementById('editField').value = c.field || '';
    document.getElementById('editStatus').value = c.status || '未建联';
    document.getElementById('editNextFollowUp').value = (c.next_follow_up || '').substring(0, 10);
    document.getElementById('editWebsite').value = c.website || '';
    document.getElementById('editProfile').value = c.profile || '';
    document.getElementById('editNotes').value = c.notes || '';
    renderContacts(c.contacts || []);
    renderOutreach(c.outreach_emails || []);
    renderResearch(c.research, c.id);
    document.querySelectorAll('#customerEditModal .tab-btn').forEach(function(t, i) { t.classList.toggle('active', i === 0); });
    document.querySelectorAll('#customerEditModal .tab-content').forEach(function(t, i) { t.classList.toggle('active', i === 0); });
    openModal('customerEditModal');
  } catch(e) {
    showToast('加载客户信息失败: ' + (e.message || '未知错误'), 'error');
  }
}

async function saveCustomer() {
  var id = document.getElementById('editCustomerId').value;
  var name = document.getElementById('editName').value.trim();
  if (!name) { showToast('客户名称不能为空', 'warning'); return; }
  
  var country = getSelectedCountries('editCountryContainer');
  document.getElementById('editCountry').value = country;
  
  var data = {
    name: name,
    company: document.getElementById('editCompany').value.trim(),
    country: country,
    level: document.getElementById('editLevel').value,
    type: document.getElementById('editType').value,
    field: document.getElementById('editField').value.trim(),
    status: document.getElementById('editStatus').value,
    next_follow_up: document.getElementById('editNextFollowUp').value,
    website: document.getElementById('editWebsite').value.trim(),
    profile: document.getElementById('editProfile').value.trim(),
    notes: document.getElementById('editNotes').value.trim()
  };
  try {
    await api('/api/customers/' + id, { method: 'PUT', body: JSON.stringify(data) });
    showToast('客户更新成功', 'success');
    closeModal('customerEditModal');
    if (currentPage === 'customers') loadCustomers();
    else if (currentPage === 'newpool') loadNewPool();
    else loadDashboard();
  } catch(e) {}
}

async function deleteCustomer(id) {
  if (!confirm('Are you sure you want to delete this client?')) return;
  try {
    await api('/api/customers/' + id, { method: 'DELETE' });
    showToast('客户已删除', 'success');
    if (currentPage === 'customers') loadCustomers();
    else if (currentPage === 'newpool') loadNewPool();
    else loadDashboard();
  } catch(e) {}
}

// Contacts
function renderContacts(contacts) {
  var el = document.getElementById('contactsList');
  if (!contacts || contacts.length === 0) { el.innerHTML = '<div class="empty-state" style="padding:30px;"><p>No contacts added</p></div>'; return; }
  var html = '';
  contacts.forEach(function(c) {
    html += '<div class="sub-item"><div class="sub-item-header"><span class="sub-item-title">' + (c.name || '') + (c.is_primary ? ' <span style="font-size:0.7rem;color:var(--accent);">(Primary)</span>' : '') + '</span><button class="btn btn-sm btn-danger" onclick="deleteContact(' + c.id + ')">Delete</button></div><div class="sub-item-detail">' +
      (c.title ? '<div>' + c.title + '</div>' : '') + (c.email ? '<div>' + c.email + '</div>' : '') +
      (c.phone ? '<div>' + c.phone + '</div>' : '') + (c.linkedin ? '<div>' + c.linkedin + '</div>' : '') + '</div></div>';
  });
  el.innerHTML = html;
}

async function addContact() {
  var id = document.getElementById('editCustomerId').value;
  var name = document.getElementById('contactName').value.trim();
  if (!name) { showToast('联系人姓名不能为空', 'warning'); return; }
  var data = { name: name, title: document.getElementById('contactTitle').value.trim(), email: document.getElementById('contactEmail').value.trim(), phone: document.getElementById('contactPhone').value.trim(), linkedin: document.getElementById('contactLinkedin').value.trim() };
  try {
    await api('/api/customers/' + id + '/contacts', { method: 'POST', body: JSON.stringify(data) });
    showToast('联系人已添加', 'success');
    document.getElementById('contactName').value = ''; document.getElementById('contactTitle').value = '';
    document.getElementById('contactEmail').value = ''; document.getElementById('contactPhone').value = ''; document.getElementById('contactLinkedin').value = '';
    var c = await api('/api/customers/' + id); renderContacts(c.contacts || []);
  } catch(e) {}
}

async function deleteContact(contactId) {
  if (!confirm('确认删除该联系人?')) return;
  try {
    await api('/api/contacts/' + contactId, { method: 'DELETE' });
    showToast('联系人已删除', 'success');
    var id = document.getElementById('editCustomerId').value;
    var c = await api('/api/customers/' + id); renderContacts(c.contacts || []);
  } catch(e) {}
}

// Outreach
function renderOutreach(emails) {
  var el = document.getElementById('outreachList');
  if (!emails || emails.length === 0) { el.innerHTML = '<div class="empty-state" style="padding:30px;"><p>No outreach records</p></div>'; return; }
  var html = '';
  emails.forEach(function(e) {
    html += '<div class="sub-item"><div class="sub-item-header"><span class="sub-item-title">' + (e.subject || '(No subject)') + '</span><div style="display:flex;gap:6px;align-items:center;">' + statusBadge(e.reply_status) + '<button class="btn btn-sm btn-danger" onclick="deleteOutreach(' + e.id + ')">Delete</button></div></div><div class="sub-item-detail"><div>Sent: ' + formatDate(e.sent_date) + '</div>' +
      (e.content ? '<div style="margin-top:4px;font-size:0.8rem;color:var(--fg-muted);">' + e.content.substring(0, 150) + (e.content.length > 150 ? '...' : '') + '</div>' : '') + '</div></div>';
  });
  el.innerHTML = html;
}

async function addOutreach() {
  var id = document.getElementById('editCustomerId').value;
  var subject = document.getElementById('outreachSubject').value.trim();
  if (!subject) { showToast('请填写主题', 'warning'); return; }
  var data = { subject: subject, content: document.getElementById('outreachContent').value.trim(), sent_date: document.getElementById('outreachDate').value, reply_status: document.getElementById('outreachReply').value };
  try {
    await api('/api/customers/' + id + '/outreach', { method: 'POST', body: JSON.stringify(data) });
    showToast('跟进记录已添加', 'success');
    document.getElementById('outreachSubject').value = ''; document.getElementById('outreachContent').value = '';
    document.getElementById('outreachDate').value = ''; document.getElementById('outreachReply').value = 'pending';
    var c = await api('/api/customers/' + id); renderOutreach(c.outreach_emails || []);
  } catch(e) {}
}

async function deleteOutreach(outreachId) {
  if (!confirm('Delete this outreach record?')) return;
  try {
    await api('/api/outreach/' + outreachId, { method: 'DELETE' });
    showToast('记录已删除', 'success');
    var id = document.getElementById('editCustomerId').value;
    var c = await api('/api/customers/' + id); renderOutreach(c.outreach_emails || []);
  } catch(e) {}
}

// Research
function renderResearch(research, customerId) {
  var el = document.getElementById('researchContent');
  if (!research) {
    el.innerHTML = '<div class="empty-state" style="padding:30px;"><div class="empty-icon">&#9830;</div><p>暂无调研报告</p><button class="btn btn-primary" style="margin-top:16px;" onclick="openResearchModal(' + customerId + ')">生成调研报告</button></div>';
    return;
  }
  var html = '<div class="research-panel"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;"><h4 style="font-family:var(--font-display);font-weight:600;">调研报告</h4><div style="display:flex;gap:8px;"><button class="btn btn-sm" onclick="openResearchModal(' + customerId + ')">重新生成</button><button class="btn btn-sm btn-danger" onclick="deleteResearch(' + customerId + ')">删除</button></div></div>';
  if (research.summary) html += '<div class="research-section"><h5>摘要</h5><p>' + research.summary + '</p></div>';
  if (research.company_info) html += '<div class="research-section"><h5>公司信息</h5><p>' + research.company_info + '</p></div>';
  if (research.key_findings) html += '<div class="research-section"><h5>关键发现</h5><p>' + research.key_findings + '</p></div>';
  if (research.needs_analysis) html += '<div class="research-section"><h5>需求分析</h5><p>' + research.needs_analysis + '</p></div>';
  if (research.cooperation_value) html += '<div class="research-section"><h5>合作价值</h5><p>' + research.cooperation_value + '</p></div>';
  html += '</div>';
  el.innerHTML = html;
}

function openResearchModal(customerId) {
  document.getElementById('researchCustomerId').value = customerId;
  var nameEl = document.getElementById('editName');
  document.getElementById('researchCompanyName').value = nameEl ? nameEl.value : '';
  document.getElementById('researchRawInput').value = '';
  openModal('researchModal');
}

async function generateResearch() {
  var customerId = document.getElementById('researchCustomerId').value;
  var rawInput = document.getElementById('researchRawInput').value.trim();
  if (!rawInput) { showToast('请输入调研内容', 'warning'); return; }
  var companyName = document.getElementById('researchCompanyName').value.trim();
  try {
    var report = await api('/api/research/generate', { method: 'POST', body: JSON.stringify({ raw_input: rawInput, company_name: companyName }) });
    await api('/api/customers/' + customerId + '/research', { method: 'POST', body: JSON.stringify(report) });
    showToast('调研报告已生成', 'success');
    closeModal('researchModal');
    var c = await api('/api/customers/' + customerId);
    renderResearch(c.research, customerId);
  } catch(e) {}
}

async function deleteResearch(customerId) {
  if (!confirm('确认删除该调研报告?')) return;
  try {
    await api('/api/customers/' + customerId + '/research', { method: 'DELETE' });
    showToast('调研报告已删除', 'success');
    renderResearch(null, customerId);
  } catch(e) {}
}

// ========== COMPLETE REMINDER ==========
function openCompleteModal(reminderId) {
  api('/api/reminders/today').then(function(reminders) {
    var r = reminders.find(function(r) { return r.id === reminderId; });
    if (!r) {
      // also try upcoming
      api('/api/reminders/upcoming').then(function(upcoming) {
        r = upcoming.find(function(r) { return r.id === reminderId; });
        if (!r) return;
        fillCompleteModal(r);
      });
      return;
    }
    fillCompleteModal(r);
  });
}

function fillCompleteModal(r) {
  document.getElementById('completeReminderId').value = r.id;
  document.getElementById('completeCustomerName').textContent = r.customer_name || '';
  document.getElementById('completeContent').textContent = r.content || '';
  document.getElementById('completeResult').value = '';
  document.getElementById('completeNextFollow').value = '';
  openModal('completeModal');
}

async function submitComplete() {
  var id = document.getElementById('completeReminderId').value;
  var nextDate = document.getElementById('completeNextFollow').value.trim();
  if (!nextDate) { showToast('请选择下次跟进日期', 'warning'); return; }
  var data = {
    result: document.getElementById('completeResult').value.trim(),
    next_follow_up: nextDate
  };
  try {
    await api('/api/reminders/' + id, { method: 'PUT', body: JSON.stringify(data) });
    showToast('跟进已记录，下次已安排', 'success');
    closeModal('completeModal');
    if (currentPage === 'dashboard') loadDashboard();
    else if (currentPage === 'calendar') loadCalendar();
  } catch(e) {}
}

// ========== ADD CUSTOMER MODALS ==========
function openAddCustomerModal() {
  document.getElementById('addExistName').value = '';
  document.getElementById('addExistWebsite').value = '';
  document.getElementById('addExistLevel').value = 'C';
  document.getElementById('addExistType').value = '';
  document.getElementById('addExistField').value = '';
  document.getElementById('addExistStatus').value = '跟进中';
  document.getElementById('addExistNextFollow').value = '';
  document.getElementById('addExistProfile').value = '';
  document.getElementById('addExistNotes').value = '';
  // 清除所有国家复选框
  document.querySelectorAll('#addExistCountryContainer input[type="checkbox"]').forEach(function(cb) {
    cb.checked = false;
  });
  document.getElementById('addExistCountry').value = '';
  openModal('addCustomerModal');
}

function getSelectedCountries(containerId) {
  var selected = [];
  document.querySelectorAll('#' + containerId + ' input[type="checkbox"]:checked').forEach(function(cb) {
    selected.push(cb.value);
  });
  return selected.join(', ');
}

function setSelectedCountries(containerId, countries) {
  document.querySelectorAll('#' + containerId + ' input[type="checkbox"]').forEach(function(cb) {
    cb.checked = false;
  });
  if (!countries) return;
  var countryList = countries.split(',').map(function(c) { return c.trim(); });
  document.querySelectorAll('#' + containerId + ' input[type="checkbox"]').forEach(function(cb) {
    if (countryList.includes(cb.value)) {
      cb.checked = true;
    }
  });
}

async function smartFillCustomer(type) {
  var company = document.getElementById('addExistName').value.trim();
  var website = document.getElementById('addExistWebsite').value.trim();
  
  if (!company && !website) {
    showToast('请先输入公司名称或网站', 'warning');
    return;
  }
  
  try {
    var result = await api('/api/customers/smart-import', { 
      method: 'POST', 
      body: JSON.stringify({ company: company, website: website }) 
    });
    
    if (result.name && !company) {
      document.getElementById('addExistName').value = result.name;
    }
    if (result.website && !website) {
      document.getElementById('addExistWebsite').value = result.website;
    }
    if (result.country) {
      setSelectedCountries('addExistCountryContainer', result.country);
      document.getElementById('addExistCountry').value = result.country;
    }
    if (result.field && !document.getElementById('addExistField').value) {
      document.getElementById('addExistField').value = result.field;
    }
    
    var filledFields = result.auto_filled || [];
    if (filledFields.length > 0) {
      showToast('Auto-filled: ' + filledFields.join(', '), 'success');
    } else {
      showToast('未找到自动填充建议', 'info');
    }
  } catch(e) {
    showToast('智能填充失败', 'error');
  }
}

async function submitExistCustomer() {
  var name = document.getElementById('addExistName').value.trim();
  if (!name) { showToast('请填写公司名称', 'warning'); return; }
  
  var country = getSelectedCountries('addExistCountryContainer');
  document.getElementById('addExistCountry').value = country;
  
  var data = {
    name: name, company: name,
    country: country,
    level: document.getElementById('addExistLevel').value,
    type: document.getElementById('addExistType').value,
    field: document.getElementById('addExistField').value.trim(),
    status: document.getElementById('addExistStatus').value || '跟进中',
    next_follow_up: document.getElementById('addExistNextFollow').value,
    website: document.getElementById('addExistWebsite').value.trim(),
    profile: document.getElementById('addExistProfile').value.trim(),
    notes: document.getElementById('addExistNotes').value.trim(),
    customer_type: 'existing'
  };
  try {
    await api('/api/customers', { method: 'POST', body: JSON.stringify(data) });
    showToast('客户添加成功', 'success');
    closeModal('addCustomerModal');
    if (currentPage === 'customers') loadCustomers(); else loadDashboard();
  } catch(e) {}
}

function openAddNewCustomerModal() {
  document.getElementById('newCustomerName').value = '';
  document.getElementById('newCustomerCountry').value = '';
  document.getElementById('newCustomerField').value = '';
  document.getElementById('newCustomerWebsite').value = '';
  document.getElementById('newCustomerNotes').value = '';
  openModal('addNewCustomerModal');
}

async function submitNewCustomer() {
  var name = document.getElementById('newCustomerName').value.trim();
  if (!name) { showToast('请填写公司名称', 'warning'); return; }
  var data = {
    name: name, company: name,
    country: document.getElementById('newCustomerCountry').value.trim(),
    field: document.getElementById('newCustomerField').value.trim(),
    website: document.getElementById('newCustomerWebsite').value.trim(),
    notes: document.getElementById('newCustomerNotes').value.trim(),
    customer_type: 'new'
  };
  try {
    await api('/api/customers', { method: 'POST', body: JSON.stringify(data) });
    showToast('新客户已添加，自动设置15/30/60天提醒', 'success');
    closeModal('addNewCustomerModal');
    if (currentPage === 'newpool') loadNewPool(); else loadDashboard();
  } catch(e) {}
}

function openBatchAddModal() {
  document.getElementById('batchAddText').value = '';
  openModal('batchAddModal');
}

async function submitBatchAdd() {
  var text = document.getElementById('batchAddText').value.trim();
  if (!text) { showToast('请输入客户数据', 'warning'); return; }
  var lines = text.split('\n').filter(function(l) { return l.trim(); });
  var count = 0;
  for (var i = 0; i < lines.length; i++) {
    var parts = lines[i].split(',').map(function(s) { return s.trim(); });
    if (parts[0]) {
      try {
        await api('/api/customers', { method: 'POST', body: JSON.stringify({ name: parts[0], company: parts[1] || '', country: parts[2] || '', field: parts[3] || '', notes: parts[4] || '', customer_type: 'new' }) });
        count++;
      } catch(e) {}
    }
  }
  showToast('Added ' + count + ' new clients', 'success');
  closeModal('batchAddModal');
  if (currentPage === 'newpool') loadNewPool(); else loadDashboard();
}

// ========== CALENDAR ==========
async function loadCalendar() {
  try {
    var reminders = await api('/api/reminders/today');
    var upcoming = await api('/api/reminders/upcoming');
    calendarData = {};
    var all = reminders.concat(upcoming);
    all.forEach(function(r) {
      var d = r.remind_date ? r.remind_date.substring(0, 10) : '';
      if (!d) return;
      if (!calendarData[d]) calendarData[d] = [];
      calendarData[d].push(r);
    });
    renderCalendar();
  } catch(e) { calendarData = {}; renderCalendar(); }
}

function renderCalendar() {
  var grid = document.getElementById('calendarGrid');
  var title = document.getElementById('calendarTitle');
  var months = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  title.textContent = months[calendarMonth] + ' ' + calendarYear;
  var dayNames = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  var html = '';
  dayNames.forEach(function(d) { html += '<div class="calendar-day-header">' + d + '</div>'; });
  var firstDay = new Date(calendarYear, calendarMonth, 1).getDay();
  var daysInMonth = new Date(calendarYear, calendarMonth + 1, 0).getDate();
  var daysInPrevMonth = new Date(calendarYear, calendarMonth, 0).getDate();
  var today = new Date();
  var todayStr = today.getFullYear() + '-' + String(today.getMonth() + 1).padStart(2, '0') + '-' + String(today.getDate()).padStart(2, '0');
  for (var i = firstDay - 1; i >= 0; i--) {
    var d = daysInPrevMonth - i;
    html += '<div class="calendar-day other-month"><div class="day-num">' + d + '</div></div>';
  }
  for (var d = 1; d <= daysInMonth; d++) {
    var dateStr = calendarYear + '-' + String(calendarMonth + 1).padStart(2, '0') + '-' + String(d).padStart(2, '0');
    var isToday = dateStr === todayStr;
    var tasks = calendarData[dateStr] || [];
    var hasOverdue = tasks.some(function(t) { return t.remind_date < todayStr; });
    html += '<div class="calendar-day' + (isToday ? ' today' : '') + '" onclick="showCalendarDetail(\'' + dateStr + '\')">' +
      '<div class="day-num">' + d + '</div>' +
      (tasks.length > 0 ? '<div class="day-tasks' + (hasOverdue ? ' overdue' : '') + '">' + tasks.length + ' task' + (tasks.length > 1 ? 's' : '') + '</div>' : '') + '</div>';
  }
  var totalCells = firstDay + daysInMonth;
  var remaining = (7 - (totalCells % 7)) % 7;
  for (var d = 1; d <= remaining; d++) {
    html += '<div class="calendar-day other-month"><div class="day-num">' + d + '</div></div>';
  }
  grid.innerHTML = html;
  document.getElementById('calendarDetail').innerHTML = '';
}

function showCalendarDetail(dateStr) {
  var tasks = calendarData[dateStr] || [];
  var el = document.getElementById('calendarDetail');
  if (tasks.length === 0) {
    el.innerHTML = '<div class="card" style="margin-top:16px;"><div class="card-body"><div class="empty-state" style="padding:30px;"><p>No tasks for ' + dateStr + '</p></div></div></div>';
    return;
  }
  var html = '<div class="card" style="margin-top:16px;"><div class="card-header"><h3>Tasks for ' + dateStr + ' (' + tasks.length + ')</h3></div><div class="card-body">';
  tasks.forEach(function(r) {
    var overdue = r.remind_date < new Date().toISOString().split('T')[0];
    var basicInfo = '<span style="color:var(--fg-secondary);font-size:0.85rem;font-weight:600;">' + (r.customer_company || r.customer_name || 'Unknown') + '</span>';
    if (r.country) basicInfo += ' <span style="color:var(--fg-light);margin:0 4px;">·</span> <span style="color:var(--fg-secondary);font-size:0.82rem;">' + r.country + '</span>';
    if (r.customer_type === '中间商' || r.customer_type === '终端') basicInfo += ' <span class="badge" style="background:var(--bg-warm);color:var(--fg-muted);border-color:var(--border);padding:1px 6px;font-size:0.68rem;">' + r.customer_type + '</span>';
    if (r.field) basicInfo += ' <span style="color:var(--fg-light);margin:0 4px;">·</span> <span style="color:var(--fg-muted);font-size:0.78rem;">' + r.field + '</span>';
    var profileHtml = r.profile ? '<div style="font-size:0.78rem;color:var(--fg-muted);margin-top:4px;line-height:1.5;">' + escapeHtml(r.profile) + '</div>' : '';
    var lastContactHtml = r.last_contact ? '<div style="font-size:0.75rem;color:var(--fg-light);margin-top:4px;">&#128337; Last: <span style="color:var(--fg-muted);">' + formatDate(r.last_contact) + '</span></div>' : '';
    html += '<div class="reminder-item"><div class="reminder-info"><div>' + basicInfo + '</div>' + profileHtml + lastContactHtml + '<div style="margin-top:6px;"><span class="reminder-content">' + escapeHtml(r.content || '') + '</span></div></div>' +
      '<div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px;flex-shrink:0;">' +
      (overdue ? '<span class="badge badge-overdue">Overdue</span>' : '') + (r.level ? levelBadge(r.level) : '') + '<button class="btn btn-sm" onclick="openCompleteModal(' + r.id + ')">Log Follow-up</button></div></div>';
  });
  html += '</div></div>';
  el.innerHTML = html;
}

function changeMonth(delta) {
  calendarMonth += delta;
  if (calendarMonth > 11) { calendarMonth = 0; calendarYear++; }
  if (calendarMonth < 0) { calendarMonth = 11; calendarYear--; }
  loadCalendar();
}

function goToday() {
  var now = new Date(); calendarYear = now.getFullYear(); calendarMonth = now.getMonth(); loadCalendar();
}

function exportCalendarICS() {
  if (Object.keys(calendarData).length === 0) { showToast('没有可导出的任务', 'warning'); return; }
  var ics = 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//CRM Follow-up//EN\r\n';
  for (var date in calendarData) {
    var tasks = calendarData[date];
    tasks.forEach(function(t) {
      var d = date.replace(/-/g, '');
      ics += 'BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:' + d + '\r\nDTEND;VALUE=DATE:' + d + '\r\nSUMMARY:' + (t.customer_name || 'Follow-up') + '\r\nDESCRIPTION:' + (t.content || '').replace(/[,;\\n]/g, ' ') + '\r\nEND:VEVENT\r\n';
    });
  }
  ics += 'END:VCALENDAR';
  var blob = new Blob([ics], { type: 'text/calendar;charset=utf-8' });
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a'); a.href = url; a.download = 'followup_calendar.ics'; a.click();
  URL.revokeObjectURL(url);
  showToast('日历导出成功', 'success');
}

// ========== CALENDAR SUBSCRIPTION (iCal) ==========
function initIcalUrl() {
  var input = document.getElementById('icalUrlInput');
  if (!input) return;
  input.value = '正在检测网络...';
  api('/api/network/ip').then(function(net) {
    input.value = net.subscribe_url || (window.location.protocol + '//' + window.location.host + '/api/calendar/ical');
  }).catch(function() {
    input.value = window.location.protocol + '//' + window.location.host + '/api/calendar/ical';
  });
}

function copyIcalUrl() {
  var input = document.getElementById('icalUrlInput');
  if (!input || !input.value) return;
  input.select();
  try {
    document.execCommand('copy');
    showToast('订阅链接已复制', 'success');
  } catch(e) {
    showToast('复制失败，请手动复制', 'error');
  }
}

function showIcalHelp() {
  var helpHtml = '<div style="max-width:500px;">' +
    '<h3 style="font-weight:600;margin-bottom:14px;">在 iPhone 上订阅日历</h3>' +
    '<ol style="margin:0;padding-left:20px;line-height:1.8;font-size:0.85rem;color:var(--fg-secondary);">' +
    '<li>打开 iPhone <strong>设置</strong> App</li>' +
    '<li>轻点 <strong>App</strong> → <strong>日历</strong></li>' +
    '<li>轻点 <strong>日历账户</strong> → <strong>添加账户</strong></li>' +
    '<li>轻点 <strong>其他</strong> → <strong>添加已订阅的日历</strong></li>' +
    '<li>粘贴上方链接，轻点 <strong>下一步</strong></li>' +
    '</ol>' +
    '<p style="margin-top:14px;font-size:0.82rem;color:var(--fg-muted);">订阅后如有新提醒添加到系统，手机会自动同步更新。电脑和手机须在同一 Wi-Fi 网络下。</p>' +
    '<p style="font-size:0.82rem;color:var(--fg-muted);">如验证失败：先在 iPhone Safari 浏览器中打开链接测试能否访问，如无法访问请检查防火墙设置。</p>' +
    '</div>';
  
  // 用已有的自定义 modal 展示
  showCustomModal('日历订阅说明', helpHtml);
}

function showCustomModal(title, bodyHtml) {
  // 创建一个轻量 modal 显示帮助内容
  var overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.style.display = 'flex';
  overlay.innerHTML = '<div class="modal" style="max-width:560px;">' +
    '<div class="modal-header"><h3>' + title + '</h3><button class="modal-close" onclick="this.closest(\'.modal-overlay\').remove()">&times;</button></div>' +
    '<div class="modal-body">' + bodyHtml + '</div>' +
    '<div class="modal-footer"><button class="btn" onclick="this.closest(\'.modal-overlay\').remove()">关闭</button></div>' +
    '</div>';
  overlay.addEventListener('click', function(e) { if (e.target === this) this.remove(); });
  document.body.appendChild(overlay);
}

// ========== FOLLOW-UP HISTORY ==========
async function loadHistory() {
  try {
    var history = await api('/api/follow-history');
    var el = document.getElementById('historyTimeline');
    if (!history || history.length === 0) { el.innerHTML = '<div class="empty-state"><div class="empty-icon">&#9776;</div><p>暂无跟进历史</p></div>'; return; }
    var html = '<div class="timeline">';
    history.forEach(function(h) {
      html += '<div class="timeline-item"><div class="timeline-date">' + (h.follow_date || '') + (h.created_at ? ' | ' + h.created_at.substring(11, 16) : '') + '</div><div class="timeline-title">' + escapeHtml(h.customer_name || 'Unknown') + '</div><div class="timeline-desc">' + escapeHtml(h.content || '') + '</div>' +
        (h.result ? '<div class="timeline-desc" style="margin-top:2px;"><strong>Result:</strong> ' + escapeHtml(h.result) + '</div>' : '') +
        (h.next_plan ? '<div class="timeline-desc" style="margin-top:2px;"><strong>Next Plan:</strong> ' + escapeHtml(h.next_plan) + '</div>' : '') +
        '<div style="margin-top:8px;display:flex;gap:6px;">' +
          '<button class="btn btn-sm" onclick="openFollowEditModal(' + h.id + ')">&#9998; 编辑</button>' +
          '<button class="btn btn-sm btn-danger" onclick="deleteFollowLog(' + h.id + ')" style="font-size:0.72rem;">删除</button>' +
        '</div></div>';
    });
    html += '</div>';
    el.innerHTML = html;
  } catch(e) {}
}

// Follow-up history: edit & delete (stored in memory for modal use)
var _followCache = {};

async function openFollowEditModal(logId) {
  try {
    // Fetch fresh data for this log entry
    var history = await api('/api/follow-history');
    var log = history.find(function(h) { return h.id === logId; });
    if (!log) { showToast('记录未找到', 'error'); return; }
    _followCache = log;
    document.getElementById('followEditId').value = log.id;
    document.getElementById('followEditCustomer').value = log.customer_name || 'Unknown';
    document.getElementById('followEditDate').value = (log.follow_date || '').substring(0, 10);
    document.getElementById('followEditContent').value = log.content || '';
    document.getElementById('followEditResult').value = log.result || '';
    document.getElementById('followEditNextPlan').value = log.next_plan || '';
    openModal('followEditModal');
  } catch(e) {}
}

async function saveFollowEdit() {
  var id = document.getElementById('followEditId').value;
  var data = {
    follow_date: document.getElementById('followEditDate').value,
    content: document.getElementById('followEditContent').value.trim(),
    result: document.getElementById('followEditResult').value.trim(),
    next_plan: document.getElementById('followEditNextPlan').value.trim()
  };
  try {
    await api('/api/follow-history/' + id, { method: 'PUT', body: JSON.stringify(data) });
    showToast('跟进记录已更新', 'success');
    closeModal('followEditModal');
    loadHistory();
  } catch(e) { showToast('更新失败', 'error'); }
}

async function deleteFollowLog(logId) {
  if (!confirm('确认删除此跟进记录？此操作不可撤销。')) return;
  try {
    await api('/api/follow-history/' + logId, { method: 'DELETE' });
    showToast('跟进记录已删除', 'success');
    loadHistory();
  } catch(e) { showToast('删除失败', 'error'); }
}

// ========== ACTIVITY LOGS ==========
async function loadLogs(action) {
  try {
    var url = '/api/logs?limit=100';
    if (action && action !== 'all') url += '&action=' + encodeURIComponent(action);
    var logs = await api(url);
    var el = document.getElementById('logsList');
    
    // Always show filter buttons first
    var actions_list = ['all', 'CREATE', 'UPDATE', 'DELETE', 'COMPLETE', 'SYNC'];
    var labels = ['全部', '创建', '更新', '删除', '完成', '同步'];
    var html = '<div style="margin-bottom:12px;display:flex;gap:6px;flex-wrap:wrap;">';
    actions_list.forEach(function(a, i) {
      var isActive = a === (action || 'all');
      html += '<button class="btn btn-sm' + (isActive ? ' btn-primary' : '') + '" onclick="loadLogs(\'' + a + '\')" style="font-size:0.72rem;">' + labels[i] + '</button>';
    });
    html += '</div>';
    
    // Then show log entries (or empty state)
    if (!logs || logs.length === 0) {
      html += '<div class="empty-state"><div class="empty-icon">&#9881;</div><p>暂无操作日志</p></div>';
      el.innerHTML = html;
      return;
    }
    logs.forEach(function(l) {
      var badgeClass = '';
      if (l.action === 'create') badgeClass = 'badge-today';
      else if (l.action === 'update') badgeClass = 'badge-info';
      else if (l.action === 'delete') badgeClass = 'badge-overdue';
      else if (l.action === 'complete') badgeClass = '';
      html += '<div class="log-item"><span class="log-time">' + (l.created_at || '') + '</span><span class="badge' + (badgeClass ? ' ' + badgeClass : '') + '" style="font-size:0.68rem;">' + (l.action || '') + '</span><span class="log-detail">' + escapeHtml(l.details || '') + '</span></div>';
    });
    el.innerHTML = html;
  } catch(e) {}
}

// ========== SETTINGS ==========
async function loadSettings() {
  try {
    var sys = await api('/api/system');
    document.getElementById('systemInfo').innerHTML =
      '<div class="settings-row"><span class="label">数据库路径</span><span class="value" style="font-size:0.78rem;">' + (sys.db_path || '-') + '</span></div>' +
      '<div class="settings-row"><span class="label">客户总数（不含已删除）</span><span class="value">' + (sys.customer_count || 0) + '</span></div>' +
      '<div class="settings-row"><span class="label">调度器</span><span class="value" style="color:' + (sys.scheduler_running ? 'var(--success)' : 'var(--danger)') + ';">' + (sys.scheduler_running ? '运行中' : '未运行') + '</span></div>';
  } catch(e) {}
}

async function runHealthCheck() {
  var panel = document.getElementById('healthCheckPanel');
  panel.innerHTML = '<div class="empty-state"><p style="color:var(--fg-muted);">正在诊断系统健康状态...</p></div>';
  try {
    var health = await api('/api/health');
    var statusColors = { 'ok': 'var(--success)', 'warning': 'var(--warning)', 'error': 'var(--danger)', 'info': 'var(--fg-muted)' };
    var statusLabels = { 'ok': '正常', 'warning': '警告', 'error': '错误', 'info': '信息' };
    var overallLabel = health.overall === 'healthy' ? '系统健康' : '存在问题';
    var overallColor = health.overall === 'healthy' ? 'var(--success)' : 'var(--danger)';
    
    var html = '<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;padding:12px 16px;background:var(--bg);border-radius:var(--radius-sm);border-left:4px solid ' + overallColor + ';">' +
      '<span style="width:10px;height:10px;border-radius:50%;background:' + overallColor + ';flex-shrink:0;"></span>' +
      '<span style="font-weight:600;color:' + overallColor + ';font-size:0.9rem;">' + overallLabel + '</span>' +
      '<span style="color:var(--fg-muted);font-size:0.78rem;margin-left:auto;">' + (health.timestamp || '') + '</span></div>';
    
    health.checks.forEach(function(check) {
      var c = statusColors[check.status] || 'var(--fg-light)';
      var l = statusLabels[check.status] || check.status;
      html += '<div style="display:flex;align-items:flex-start;gap:10px;padding:10px 0;border-bottom:1px solid var(--border);">' +
        '<span style="width:8px;height:8px;border-radius:50%;background:' + c + ';flex-shrink:0;margin-top:4px;"></span>' +
        '<div style="flex:1;"><div style="display:flex;align-items:center;gap:6px;"><span style="font-weight:600;color:var(--fg-primary);font-size:0.85rem;">' + check.name + '</span><span style="font-size:0.7rem;color:' + c + ';font-weight:500;">[' + l + ']</span></div>' +
        '<div style="color:var(--fg-muted);font-size:0.78rem;margin-top:2px;">' + escapeHtml(check.detail || '') + '</div></div></div>';
    });
    
    html += '<div style="margin-top:12px;"><button class="btn btn-sm" onclick="runHealthCheck()" style="font-size:0.78rem;">重新检测</button></div>';
    panel.innerHTML = html;
  } catch(e) {
    panel.innerHTML = '<div class="empty-state"><p style="color:var(--danger);">健康检测失败: ' + escapeHtml(e.message || '网络错误') + '</p><button class="btn btn-sm" onclick="runHealthCheck()" style="margin-top:10px;">重新检测</button></div>';
  }
}

// ========== MODAL HELPERS ==========
function openModal(id) { document.getElementById(id).classList.add('show'); document.body.style.overflow = 'hidden'; }
function closeModal(id) { document.getElementById(id).classList.remove('show'); document.body.style.overflow = ''; }

document.querySelectorAll('.modal-overlay').forEach(function(overlay) {
  overlay.addEventListener('click', function(e) { if (e.target === this) { this.classList.remove('show'); document.body.style.overflow = ''; } });
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') { document.querySelectorAll('.modal-overlay.show').forEach(function(m) { m.classList.remove('show'); }); document.body.style.overflow = ''; }
});

// ========== TAB HELPER ==========
function switchTab(btn, tabId) {
  var parent = btn.closest('.modal-body');
  parent.querySelectorAll('.tab-btn').forEach(function(t) { t.classList.remove('active'); });
  parent.querySelectorAll('.tab-content').forEach(function(t) { t.classList.remove('active'); });
  btn.classList.add('active');
  document.getElementById(tabId).classList.add('active');
}
