import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

// Test-only loopback backend. Production Vite config is untouched.
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: true,
    proxy: { '/api': { target: 'http://127.0.0.1:18081', changeOrigin: false } },
  },
});
