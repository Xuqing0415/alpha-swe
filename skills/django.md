# @skill(django)
# Django 后端开发技能模块

## Django 编码规范
- 遵循 Django 的 MTV 架构（Model-Template-View）
- 使用 Django ORM 而非原生 SQL
- 在 models.py 中定义数据模型，使用 migrations 管理变更
- 使用 Django REST Framework 构建 API
- 在 settings.py 中管理配置，敏感信息使用环境变量

## 项目结构
```
project/
  ├── manage.py
  ├── project/
  │   ├── settings.py
  │   ├── urls.py
  │   └── wsgi.py
  └── app/
      ├── models.py
      ├── views.py
      ├── serializers.py
      └── urls.py
```

## 常用操作
- 创建迁移: `python manage.py makemigrations`
- 执行迁移: `python manage.py migrate`
- 创建超级用户: `python manage.py createsuperuser`
- 运行开发服务器: `python manage.py runserver`
- 运行测试: `python manage.py test`