# @skill(react)
# React 前端开发技能模块

## React 编码规范
- 使用函数组件 + Hooks，避免 class 组件
- 使用 useState/useEffect 管理状态
- 组件文件使用 PascalCase 命名
- 使用 PropTypes 或 TypeScript 做类型检查
- 避免在渲染中创建新对象/函数（useCallback/useMemo）

## 组件结构
```
src/components/
  ├── Button/
  │   ├── index.tsx
  │   ├── Button.test.tsx
  │   └── styles.module.css
  └── ...
```

## 常用操作
- 创建项目: `npx create-react-app` 或 `npm create vite@latest`
- 运行开发服务器: `npm start`
- 测试: `npm test`
- 构建: `npm run build`