<script setup lang="ts">
import { computed, ref } from 'vue';
import { Box, LockKeyhole, ShieldCheck } from '@lucide/vue';
import { ElMessage } from 'element-plus';
import { useAuthStore } from '../stores/auth';

const auth = useAuthStore();
const password = ref('');
const confirmation = ref('');
const setupMode = computed(() => !auth.configured);

async function submit(): Promise<void> {
  if (password.value.length < 12) {
    ElMessage.warning('管理员口令至少需要 12 个字符');
    return;
  }
  if (setupMode.value && password.value !== confirmation.value) {
    ElMessage.warning('两次输入的管理员口令不一致');
    return;
  }
  try {
    if (setupMode.value) await auth.setup(password.value);
    else await auth.login(password.value);
    password.value = '';
    confirmation.value = '';
  } catch {
    // 具体错误由 auth store 的 problem+json 脱敏结果展示。
  }
}
</script>

<template>
  <div class="auth-shell">
    <section class="auth-card">
      <div class="auth-brand">
        <span><Box :size="27" /></span>PackBreaker
      </div>
      <div v-if="auth.loading" class="auth-loading">
        <el-skeleton :rows="4" animated />
      </div>
      <template v-else-if="auth.error && !auth.status">
        <el-alert
          :title="
            auth.error.code === 'API_UNAVAILABLE' ? '无法连接 PackBreaker 后端' : '无法读取认证状态'
          "
          :description="auth.error.message"
          type="error"
          :closable="false"
          show-icon
        />
        <el-button type="primary" class="auth-submit" @click="auth.bootstrap">重新连接</el-button>
      </template>
      <template v-else>
        <div class="auth-icon"><LockKeyhole :size="23" /></div>
        <h1>{{ setupMode ? '初始化管理员' : '管理员登录' }}</h1>
        <p>
          {{
            setupMode
              ? '首次启动需要设置唯一管理员口令。口令仅以 Argon2id 强哈希形式保存。'
              : '使用本地管理员会话进入 PackBreaker。写操作同时受 CSRF 保护。'
          }}
        </p>
        <el-alert
          v-if="auth.error"
          :title="auth.error.message"
          type="error"
          :closable="false"
          show-icon
          class="auth-error"
        />
        <el-form label-position="top" @submit.prevent="submit">
          <el-form-item :label="setupMode ? '设置管理员口令' : '管理员口令'" required>
            <el-input
              v-model="password"
              type="password"
              show-password
              :autocomplete="setupMode ? 'new-password' : 'current-password'"
              minlength="12"
              maxlength="256"
              @keyup.enter="submit"
            />
          </el-form-item>
          <el-form-item v-if="setupMode" label="确认管理员口令" required>
            <el-input
              v-model="confirmation"
              type="password"
              show-password
              autocomplete="new-password"
              maxlength="256"
              @keyup.enter="submit"
            />
          </el-form-item>
          <el-button type="primary" class="auth-submit" :loading="auth.submitting" @click="submit">
            {{ setupMode ? '初始化并登录' : '登录' }}
          </el-button>
        </el-form>
        <div class="auth-safety">
          <ShieldCheck :size="16" />
          <span>Session Cookie 为 HttpOnly；CSRF Token 不写入 localStorage。</span>
        </div>
      </template>
    </section>
  </div>
</template>

<style scoped>
.auth-shell {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 28px;
  background:
    radial-gradient(circle at 20% 10%, rgba(33, 107, 229, 0.1), transparent 28%), var(--canvas);
}
.auth-card {
  width: min(440px, 100%);
  padding: 34px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 12px;
  box-shadow: 0 20px 60px rgba(30, 55, 80, 0.1);
}
.auth-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 22px;
  font-weight: 750;
}
.auth-brand > span,
.auth-icon {
  display: grid;
  place-items: center;
  color: white;
  background: var(--blue);
  border-radius: 7px;
}
.auth-brand > span {
  width: 37px;
  height: 37px;
}
.auth-icon {
  width: 42px;
  height: 42px;
  margin-top: 34px;
}
h1 {
  margin: 18px 0 8px;
  font-size: 24px;
}
p {
  color: var(--muted);
  line-height: 1.8;
  margin-bottom: 24px;
}
.auth-error {
  margin-bottom: 18px;
}
.auth-submit {
  width: 100%;
  margin-top: 6px;
}
.auth-loading {
  margin-top: 34px;
}
.auth-safety {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 24px;
  padding-top: 18px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: 11px;
}
.auth-safety svg {
  color: #168765;
  flex-shrink: 0;
}
</style>
