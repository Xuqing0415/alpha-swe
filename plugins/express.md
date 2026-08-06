---
name: express
description: Express 中间件与路由规范
priority: 5
version: "1.0.0"
triggers:
  project_dep: ['express', '@nestjs/core', 'koa']
  keywords: [express, 中间件, 路由, api, rest]
  file_ext: [.js]
---
# Express 中间件与路由规范
- 路由处理函数保持薄，业务逻辑抽到 service/controller 层
- 中间件按职责拆分：鉴权、日志、错误处理各一个
- 错误处理中间件必须有 4 个参数 (err, req, res, next)
- 异步路由错误必须捕获，统一交给错误中间件
- 请求参数使用校验库（Joi/Zod）验证后再进入业务层
- 敏感响应字段（密码/token）禁止返回到客户端