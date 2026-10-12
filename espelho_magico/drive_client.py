"""Upload e atualização de arquivos Markdown no Google Drive."""
import os
import time

from googleapiclient.http import MediaFileUpload

from . import config
from .gmail_client import google_lock
from .log import escape, logger


def escapar_query(valor):
    """Escapa valores para a sintaxe de query do Drive (aspas simples e barras invertidas)."""
    return valor.replace("\\", "\\\\").replace("'", "\\'")


def montar_query(nome_drive, nome_arquivo, pasta_id=None):
    nomes = f"(name='{escapar_query(nome_drive)}' or name='{escapar_query(nome_arquivo)}')"
    query = f"{nomes} and trashed=false"
    if pasta_id:
        query += f" and '{escapar_query(pasta_id)}' in parents"
    return query


def upload_para_drive(service, caminho_arquivo, nome_arquivo, pasta_id=None):
    if not service:
        return False

    pasta_id = pasta_id if pasta_id is not None else config.DRIVE_FOLDER_ID
    nome_drive = os.path.splitext(nome_arquivo)[0]
    query = montar_query(nome_drive, nome_arquivo, pasta_id)

    max_tentativas = 3
    for tentativa in range(1, max_tentativas + 1):
        try:
            media = MediaFileUpload(str(caminho_arquivo), mimetype="text/markdown", resumable=True)
            with google_lock:
                items = service.files().list(q=query, spaces="drive", fields="files(id, name)").execute().get("files", [])

                if items:
                    file_id = items[0]["id"]
                    logger.info(f"[cyan]➜ [Drive] Arquivo existente encontrado. Atualizando (ID: {file_id})...[/cyan]")
                    arquivo = service.files().update(
                        fileId=file_id, body={"name": nome_drive}, media_body=media, fields="id"
                    ).execute(num_retries=3)
                    logger.info(f"[bold green]✔ Atualização concluída! ID no Drive: {arquivo.get('id')}[/bold green]")
                    if len(items) > 1:
                        logger.warning(
                            f"[yellow]⚠ {len(items) - 1} duplicata(s) antiga(s) no Drive com esse nome. "
                            f"Apenas a primeira foi atualizada.[/yellow]"
                        )
                else:
                    logger.info(f"[cyan]➜ [Drive] Fazendo upload de {escape(nome_drive)} (Tentativa {tentativa}/{max_tentativas})...[/cyan]")
                    metadata = {"name": nome_drive}
                    if pasta_id:
                        metadata["parents"] = [pasta_id]
                    arquivo = service.files().create(body=metadata, media_body=media, fields="id").execute(num_retries=3)
                    logger.info(f"[bold green]✔ Upload concluído! ID no Drive: {arquivo.get('id')}[/bold green]")
            return True
        except Exception as e:
            if tentativa < max_tentativas:
                logger.warning(f"[yellow]⚠ Falha no upload ({escape(nome_arquivo)}): {escape(str(e))}. Tentando novamente em 5s...[/yellow]")
                time.sleep(5)
            else:
                logger.error(f"[bold red]✖ Erro final no upload para o Drive após {max_tentativas} tentativas: {escape(str(e))}[/bold red]")
    return False
