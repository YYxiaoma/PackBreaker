<script setup lang="ts">
import { ref } from 'vue';
import { KeyRound } from '@lucide/vue';
import { ElMessage } from 'element-plus';

import { ApiProblem } from '../api/client';
import { useAuthStore } from '../stores/auth';

const auth = useAuthStore();
const currentPassword = ref('');
const newPassword = ref('');
const confirmation = ref('');

async function submit(): Promise<void> {
  if (currentPassword.value.length < 12 || newPassword.value.length < 12) {
    ElMessage.warning('管理员密码至少需要 12 个字符');
    return;
  }
  if (newPassword.value !== confirmation.value) {
    ElMessage.warning('两次输入的新密码不一致');
    return;
  }
  if (newPassword.value === currentPassword.value) {
    ElMessage.warning('新密码不能与当前密码相同');
    return;
  }
  try {
    await auth.changePassword(currentPassword.value, newPassword.value, confirmation.value);
    currentPassword.value = '';
    newPassword.value = '';
    confirmation.value = '';
    ElMessage.success('密码已修改，请使用新密码重新登录');
  } catch (caught) {
    ElMessage.error(caught instanceof ApiProblem ? caught.message : '修改密码失败');
  }
}
</script>

<template>
  <div class="password-gate-shell">
    <section class="password-gate-card">
      <div class="password-gate-icon"><KeyRound :size="24" /></div>
      <h1>修改一次性临时密码</h1>
      <p>当前账户使用首次启动生成的一次性临时密码。完成改密前，其他管理功能保持锁定。</p>
      <el-alert
        v-if="auth.error"
        :title="auth.error.message"
        type="error"
        :closable="false"
        show-icon
        class="password-gate-error"
      />
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="当前密码" required>
          <el-input
            v-model="currentPassword"
            type="password"
            show-password
            autocomplete="current-password"
            maxlength="256"
          />
        </el-form-item>
        <el-form-item label="新密码" required>
          <el-input
            v-model="newPassword"
            type="password"
            show-password
            autocomplete="new-password"
            maxlength="256"
          />
        </el-form-item>
        <el-form-item label="确认新密码" required>
          <el-input
            v-model="confirmation"
            type="password"
            show-password
            autocomplete="new-password"
            maxlength="256"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-button
          type="primary"
          class="password-gate-submit"
          :loading="auth.submitting"
          @click="submit"
        >
          修改密码并注销全部会话
        </el-button>
      </el-form>
    </section>
  </div>
</template>

<style scoped>
.password-gate-shell {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 28px;
  background:
    radial-gradient(circle at 20% 10%, rgba(33, 107, 229, 0.1), transparent 28%), var(--canvas);
}
.password-gate-card {
  width: min(480px, 100%);
  padding: 34px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 12px;
  box-shadow: 0 20px 60px rgba(30, 55, 80, 0.1);
}
.password-gate-icon {
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  color: white;
  background: var(--blue);
  border-radius: 9px;
}
h1 {
  margin: 18px 0 8px;
  font-size: 24px;
}
p {
  margin: 0 0 24px;
  color: var(--muted);
  line-height: 1.8;
}
.password-gate-error {
  margin-bottom: 18px;
}
.password-gate-submit {
  width: 100%;
  margin-top: 6px;
}
</style>
