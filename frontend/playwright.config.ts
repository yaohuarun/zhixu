import {defineConfig} from '@playwright/test'
export default defineConfig({testDir:'./tests',timeout:60000,use:{baseURL:process.env.RAG_UI_URL||'http://127.0.0.1:5174',headless:true},reporter:'list',workers:1})
