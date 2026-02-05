from app.database import engine
from app.models import Base
from app import models

print("⚠️  Apagando tabelas antigas...")
Base.metadata.drop_all(bind=engine)

print("✨ Criando tabelas novas (com a coluna asset_type)...")
Base.metadata.create_all(bind=engine)

print("✅ Banco de dados atualizado com sucesso! Pode rodar o servidor.")