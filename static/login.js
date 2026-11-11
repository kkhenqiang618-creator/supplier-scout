'use strict';
const form = document.querySelector('#login-form');
const button = form.querySelector('button');
const message = document.querySelector('#message');
form.addEventListener('submit', async event => {
  event.preventDefault();
  button.disabled = true;
  button.textContent = '正在登录…';
  message.textContent = '';
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: form.username.value, password: form.password.value,
        next: new URLSearchParams(location.search).get('next') || '/'})
    });
    const data = await response.json();
    form.password.value = '';
    if (!response.ok) throw new Error(data.error || '登录失败，请重试');
    const next = new URL(data.next || '/', location.origin);
    location.assign(next.origin === location.origin ? next.href : '/');
  } catch (error) {
    form.password.value = '';
    message.textContent = error instanceof TypeError ? '连接失败，请检查网络后重试' : error.message;
    button.disabled = false;
    button.textContent = '登录';
    form.password.focus();
  }
});
