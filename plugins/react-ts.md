---
name: react-ts
description: React + TypeScript 编码规范
priority: 6
version: "1.0.0"
triggers:
  keywords: [react, 组件, hook, jsx, tsx, props, 前端]
  file_ext: [.tsx, .jsx, .ts]
  project_dep: [react, next, nextjs, react-dom]
---
# React + TypeScript 编码规范
- 组件使用函数组件 + Hooks，禁止 class 组件
- 组件文件使用 PascalCase 命名，组件内部类型以 Props 结尾
- 状态提升到最近公共父级，跨组件共享用 Context/状态库
- 渲染中不创建新对象/函数，回调使用 useCallback，缓存用 useMemo
- 副作用必须清理（useEffect 返回 cleanup）
- 类型使用 TypeScript 严格模式，不用 any