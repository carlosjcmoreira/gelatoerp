import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, logger
from db.cache import ttl_cache_args, invalidate_prefix
import json

@ttl_cache_args('artigos_administrativos', ttl=600)
def get_artigos_administrativos(apenas_ativos: bool = True) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        query = "SELECT id, fornecedor, produto, ativo FROM artigos_administrativos"
        if apenas_ativos:
            query += " WHERE ativo = TRUE"
        query += " ORDER BY fornecedor, produto"
        cursor.execute(query)
        rows = cursor.fetchall()
    return [{'id': r[0], 'fornecedor': r[1], 'produto': r[2], 'ativo': r[3]} for r in rows]


def add_artigo_administrativo(fornecedor: str, produto: str) -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO artigos_administrativos (fornecedor, produto) VALUES (%s, %s)", (fornecedor, produto))
            conn.commit()
            success = True
        except:
            conn.rollback()
            success = False
    invalidate_prefix('artigos_administrativos')
    return success


def update_artigo_administrativo(artigo_id: int, fornecedor: str, produto: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE artigos_administrativos SET fornecedor = %s, produto = %s WHERE id = %s", (fornecedor, produto, artigo_id))
        conn.commit()
    invalidate_prefix('artigos_administrativos')


def toggle_artigo_administrativo(artigo_id: int, ativo: bool):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE artigos_administrativos SET ativo = %s WHERE id = %s", (ativo, artigo_id))
        conn.commit()
    invalidate_prefix('artigos_administrativos')


def delete_artigo_administrativo(artigo_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM artigos_administrativos WHERE id = %s", (artigo_id,))
        conn.commit()
    invalidate_prefix('artigos_administrativos')


def seed_artigos_administrativos():
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM artigos_administrativos")
        if cursor.fetchone()[0] > 0:
            return
        artigos = [
            ('Makro', 'Canela em pó 500g'), ('Makro', 'Mel (kg)'), ('Makro', 'Granola (pacote com 800g'),
            ('Makro', 'Bolacha Maria (pacote com 800g)'), ('Makro', 'Açúcar para Café'),
            ('Makro', 'Adoçante Café'), ('Makro', 'Álcool 96% 100ml'), ('Makro', 'Esponja und.'),
            ('Makro', 'Papel vegetal'), ('Makro', 'Saco congelação 1 l'), ('Makro', 'Saco congelação 3l'),
            ('Makro', 'Cheirinho casa de banho Spray'), ('Makro', 'Lixívia Spray'),
            ('Makro', 'Detergente louça 4l'), ('Makro', 'Caixa Kraft pequena (pacote 50und.)'),
            ('Makro', 'Caixa Kraft grande (und)'), ('Makro', 'Caneta permanente preta (und)'),
            ('Makro', 'Neoblanc - Tira manchas (2l)'),
            ('Porto Higiene', 'Rolo de papel térmico 80x60x11 (caixa - 10 und.)'),
            ('Porto Higiene', 'Rolo de papel térmico 57x40x11 (cartão - 10 und.)'),
            ('Porto Higiene', 'Luva S (caixa)'), ('Porto Higiene', 'Luva M (caixa)'),
            ('Porto Higiene', 'Luva L (caixa)'), ('Porto Higiene', 'Saco de lixo 5l (pct - 10und)'),
            ('Porto Higiene', 'Saco de lixo 50l (pct -10 und)'), ('Porto Higiene', 'Saco de lixo 100l (pct - 10Und.)'),
            ('Porto Higiene', 'Película Aderente'), ('Porto Higiene', 'Pezinhos/Protetor de calçados'),
            ('Porto Higiene', 'Desengordurante (und - 5l)'), ('Porto Higiene', 'Sabonete WC (und - 5l)'),
            ('Porto Higiene', 'Abrilhantador máquina de louça (und - 5l)'),
            ('Porto Higiene', 'Desinfetante para superfícies Ecomix'),
            ('Porto Higiene', 'Detergente máquina de lavar louça (und - 5l)'),
            ('Porto Higiene', 'Desinfetante VT 10 - Álcool 70% (und - 5l)'),
            ('Porto Higiene', 'Desinfetante WC chão (und - 5l)'),
            ('Porto Higiene', 'Desinfetante chão - Bioalcool (und - 5l)'),
            ('Porto Higiene', 'Descalcificador (und - 5l)'), ('Porto Higiene', 'Guardanapos tipo L caixa'),
            ('Porto Higiene', 'Papel Autocorte (6 und)'), ('Porto Higiene', 'Papel Zigzag'),
            ('Porto Higiene', 'Papel Higiênico (12 und)'), ('Porto Higiene', 'Líquido de limpeza WC - (und - 5l)'),
            ('Porto Higiene', 'Touca preta redinha - (100 und)'),
            ('Greenpack', 'Tampas capuccino'), ('Greenpack', 'Tampa Milkshake'),
            ('Greenpack', 'Saco de papel kraft para cones'), ('Greenpack', 'Sacos de cookies'),
            ('Greenpack', 'Saco plástico para pastelaria'), ('Greenpack', 'Pratinho para Nivottos'),
            ('Greenpack', 'Copo para Água'), ('Greenpack', 'Copos takeway kraft'),
            ('Greenpack', 'Colheres Café embalada indivialmente 9cm'),
            ('Sumol Compal', 'Água sem gás 330ml'), ('Sumol Compal', 'Água com gás frize'),
            ('Sumol Compal', 'Frizze limão'), ('Sumol Compal', 'Frizze maracujá'),
            ('Sumol Compal', 'Frizze laranja'), ('Sumol Compal', 'Pepsi'),
            ('Sumol Compal', 'Pepsi zero'), ('Sumol Compal', 'Compal maga/laranja'),
            ('Sumol Compal', 'Compal pêssego'),
            ('SUVITA', 'Caixa Take Away 500g'), ('SUVITA', 'Caixa Take Away 1000g'),
            ('SUVITA', 'Caixa Take Away 1500g'), ('SUVITA', 'Pistacchio em pedaços'),
            ('SUVITA', 'Colheres para gelado'), ('SUVITA', 'Cone pequeno 40" (caixa 480 und)'),
            ('SUVITA', 'Cone Médio 45" (caixa 420 und.)'),
            ('LACTOGAL', 'Leite inteiro Gresso (l)'), ('LACTOGAL', 'Manteiga com sal Gresso (kg)'),
            ('MATOSINHOS', 'Copo mini (manga- 50und)'), ('MATOSINHOS', 'Copo pequeno (manga- 50und)'),
            ('MATOSINHOS', 'Copo médio (manga- 50und)'), ('MATOSINHOS', 'Copo grande (manga- 50und)'),
            ('MATOSINHOS', 'Copo max (manga- 50und)'), ('MATOSINHOS', 'Copo Café (manga- 50und)'),
            ('MATOSINHOS', 'Copo Cappucino/Branco Nivà (manga- 50und)'),
            ('MATOSINHOS', 'Copo Milkshake (manga- 50und)'), ('MATOSINHOS', 'Colheres brancas cartão Açaí'),
            ('MATOSINHOS', 'Guardanapos Nivà'), ('MATOSINHOS', 'Ricotta (kg)'),
            ('MATOSINHOS', 'Cannolo (und)'), ('MATOSINHOS', 'Chocolate 64% (Cannolo) - (und)'),
            ('MATOSINHOS', 'Brioche (und.)'), ('MATOSINHOS', 'Saco Nivà (und)'),
            ('MATOSINHOS', 'Fita Niva caixas Take away (rolo)'),
            ('MATOSINHOS', 'Capa para cone/Porta cones roxo'), ('MATOSINHOS', 'Base Branca Nivà'),
            ('MATOSINHOS', 'Cerejas em calda'), ('MATOSINHOS', 'Esfregona'),
            ('MATOSINHOS', 'Palhinha milkshake'), ('MATOSINHOS', 'Pote takeway p/ amarena'),
            ('Progelcone', 'Mini Cone'),
            ('CAFÉ ILLY', 'Café Classico'), ('CAFÉ ILLY', 'Descafeinado'),
            ('CAFÉ ILLY', 'Chá black'), ('CAFÉ ILLY', 'Chá lime'),
            ('CAFÉ ILLY', 'Chá citrus'), ('CAFÉ ILLY', 'Chá Red fruits'),
            ('GRÁFICA', 'Registro de temperaturas'), ('GRÁFICA', 'Limpeza WC'),
            ('GRÁFICA', 'Limpeza área clientes'), ('GRÁFICA', 'Limpeza zona de produção'),
            ('GRÁFICA', 'Rastreabilidade'), ('GRÁFICA', 'Entrada de mercadorias'),
            ('GRÁFICA', 'Fecho de caixa'),
        ]
        for forn, prod in artigos:
            cursor.execute("INSERT INTO artigos_administrativos (fornecedor, produto) VALUES (%s, %s) ON CONFLICT DO NOTHING", (forn, prod))
        conn.commit()
