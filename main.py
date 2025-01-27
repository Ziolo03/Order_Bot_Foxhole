import discord
from discord import app_commands
from discord.ext import commands
import psycopg2


class BotClient(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="/", intents=discord.Intents.all())

    async def setup_hook(self):
        await self.tree.sync()
        print("Komendy slashowe zsynchronizowane!")


bot = BotClient()

conn = psycopg2.connect(
    dbname="DBNAME",
    user="USER",
    password="PASSWORD",
    host="HOST",
    port="PORT"
)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    order_name TEXT NOT NULL,
    creator_id BIGINT NOT NULL,
    completed BOOLEAN DEFAULT FALSE
);
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_name TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    progress INTEGER DEFAULT 0,
    completed BOOLEAN DEFAULT FALSE
);
""")
conn.commit()

def load_product_names(file_path: str) -> list[str]:
    product_names = []
    with open(file_path, "r", encoding="utf-8") as file:
        for line in file:
            if line.startswith("%") or not line.strip():
                continue
            parts = line.split(" ", 3)
            if len(parts) == 4:
                product_names.append(parts[3].strip())
    return product_names


product_names = load_product_names("names.txt")


def get_order_id_from_thread(thread_id: int) -> int | None:
    cursor.execute("SELECT id FROM orders WHERE order_name = %s;", (str(thread_id),))
    result = cursor.fetchone()
    return result[0] if result else None


async def get_order_details(order_id: int) -> str:
    cursor.execute("SELECT order_name, completed FROM orders WHERE id = %s;", (order_id,))
    order = cursor.fetchone()

    if not order:
        return f"Zamówienie o ID {order_id} nie istnieje."

    order_name, completed = order
    status = "ZAKOŃCZONE" if completed else "W TRAKCIE"

    cursor.execute("""
        SELECT product_name, quantity, progress, completed
        FROM order_items
        WHERE order_id = %s
        ORDER BY product_name ASC;
    """, (order_id,))
    items = cursor.fetchall()

    if items:
        item_list = [
            f"{product_name}: {f'{progress}''/'f'{quantity}''✅' if completed else f'{progress}/{quantity}'}"
            for product_name, quantity, progress, completed in items
        ]
        return f"""
Zamówienie: {order_name} (ID: {order_id})
Status: {status}
Produkty:
{chr(10).join(item_list)}
"""
    return f"Zamówienie '{order_name}' (ID: {order_id}) nie ma jeszcze produktów."


async def update_order_status_message(thread: discord.Thread, order_id: int):
    order_details = await get_order_details(order_id)
    pinned_message = None

    async for message in thread.history(limit=10):
        if message.pinned:
            pinned_message = message
            break

    if pinned_message:
        await pinned_message.edit(content=f"```{order_details}```")
    else:
        message = await thread.send(f"```{order_details}```")
        await message.pin()


async def product_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    thread_id = interaction.channel.id
    order_id = get_order_id_from_thread(thread_id)

    if not order_id:
        return []

    cursor.execute("SELECT product_name FROM order_items WHERE order_id = %s;", (order_id,))
    products = [row[0] for row in cursor.fetchall()]
    matching_products = [product for product in products if current.lower() in product.lower()]

    return [app_commands.Choice(name=product, value=product) for product in matching_products[:20]]


async def product_name_autocomplete_add(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    matching_products = [name for name in product_names if current.lower() in name.lower()]
    return [app_commands.Choice(name=name, value=name) for name in matching_products[:20]]


@bot.tree.command(name="zamówienie_stwórz", description="Tworzy nowe zamówienie w bieżącym wątku.")
async def create_order(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message("Ta komenda może być wykonywana tylko w wątku.", ephemeral=True)
        return

    thread_id = interaction.channel.id
    if get_order_id_from_thread(thread_id):
        await interaction.response.send_message("Zamówienie dla tego wątku już istnieje.", ephemeral=True)
        return

    cursor.execute(
        "INSERT INTO orders (order_name, creator_id) VALUES (%s, %s) RETURNING id;",
        (str(thread_id), interaction.user.id)
    )
    order_id = cursor.fetchone()[0]
    conn.commit()

    await update_order_status_message(interaction.channel, order_id)
    await interaction.response.send_message(
        f"Zamówienie zostało utworzone w tym wątku (ID: {order_id}).",
        ephemeral=True
    )


@bot.tree.command(name="zamówienie_dodaj_produkt", description="Dodaje produkt do zamówienia w bieżącym wątku.")
@app_commands.autocomplete(product_name=product_name_autocomplete_add)
async def add_product(interaction: discord.Interaction, product_name: str, quantity: int):
    if quantity <= 0:
        await interaction.response.send_message("Ilość produktu musi byćwiększa od zera.",ephemeral=True)
        return
    if not (-2_147_483_648 <= quantity <= 2_147_483_647):
        await interaction.response.send_message(
            "Ilość produktu przekracza dopuszczalny zakres liczb całkowitych.", ephemeral=True
        )
        return
    
    thread_id = interaction.channel.id
    order_id = get_order_id_from_thread(thread_id)

    if not order_id:
        await interaction.response.send_message("Nie znaleziono zamówienia dla tego wątku.", ephemeral=True)
        return

    cursor.execute(
        "SELECT id FROM order_items WHERE order_id = %s AND product_name = %s;",
        (order_id, product_name)
    )
    if cursor.fetchone():
        await interaction.response.send_message(f"Produkt '{product_name}' już istnieje w zamówieniu.", ephemeral=True)
        return

    cursor.execute(
        "INSERT INTO order_items (order_id, product_name, quantity) VALUES (%s, %s, %s);",
        (order_id, product_name, quantity)
    )
    conn.commit()

    if product_name not in product_names:
        product_names.append(product_name)

    await update_order_status_message(interaction.channel, order_id)
    await interaction.response.send_message(f"Produkt '{product_name}' został dodany do zamówienia.", ephemeral=True)

@bot.tree.command(name="zamówienie_aktualizuj_produkt", description="Aktualizuje stan produktu w zamówieniu.")
@app_commands.autocomplete(product_name=product_name_autocomplete)
async def update_product(interaction: discord.Interaction, product_name: str, progress: int):
    if progress <= 0:
        await interaction.response.send_message("Ilość produktu musi byćwiększa od zera.",ephemeral=True)
        return
    if not (-2_147_483_648 <= progress <= 2_147_483_647):
        await interaction.response.send_message(
            "Ilość produktu przekracza dopuszczalny zakres liczb całkowitych.", ephemeral=True
        )
        return
    
    thread_id = interaction.channel.id
    order_id = get_order_id_from_thread(thread_id)

    if not order_id:
        await interaction.response.send_message("Nie znaleziono zamówienia dla tego wątku.", ephemeral=True)
        return

    cursor.execute(
        "SELECT id, quantity, progress FROM order_items WHERE order_id = %s AND product_name = %s AND completed = FALSE;",
        (order_id, product_name)
    )
    item = cursor.fetchone()

    if not item:
        await interaction.response.send_message(f"Produkt '{product_name}' nie istnieje lub jest już ukończony.", ephemeral=True)
        return

    product_id, quantity, current_progress = item
    new_progress = current_progress + progress

    if new_progress >= quantity:
        cursor.execute("UPDATE order_items SET progress = %s, completed = TRUE WHERE id = %s;", (quantity, product_id))
        conn.commit()
        if product_name in product_names:
            product_names.remove(product_name)
        await interaction.response.send_message(f"Produkt '{product_name}' został ukończony! ({quantity}/{quantity})", ephemeral=False)
    else:
        cursor.execute("UPDATE order_items SET progress = %s WHERE id = %s;", (new_progress, product_id))
        conn.commit()
        await interaction.response.send_message(f"Zaktualizowano stan produktu '{product_name}': {new_progress}/{quantity}.", ephemeral=False)

    await update_order_status_message(interaction.channel, order_id)

@bot.tree.command(name="zamówienie_popraw", description="Aktualizuje zażądaną ilość produktu w zamówieniu.")
@app_commands.autocomplete(product_name=product_name_autocomplete)
async def update_quantity(interaction: discord.Interaction, product_name: str, quantity: int):
    if quantity <= 0:
        await interaction.response.send_message("Ilość produktu musi być większa od zera.", ephemeral=True)
        return
    if not (-2_147_483_648 <= quantity <= 2_147_483_647):
        await interaction.response.send_message(
            "Ilość produktu przekracza dopuszczalny zakres liczb całkowitych.", ephemeral=True
        )
        return

    thread_id = interaction.channel.id
    order_id = get_order_id_from_thread(thread_id)

    if not order_id:
        await interaction.response.send_message("Nie znaleziono zamówienia dla tego wątku.", ephemeral=True)
        return

    cursor.execute(
        "SELECT id, quantity, progress FROM order_items WHERE order_id = %s AND product_name = %s AND completed = FALSE;",
        (order_id, product_name)
    )

    item = cursor.fetchone()

    if not item:
        await interaction.response.send_message(f"Produkt '{product_name}' nie istnieje lub jest już ukończony.", ephemeral=True)
        return

    product_id, current_quantity, _ = item  

    new_quantity = quantity + current_quantity
    cursor.execute("UPDATE order_items SET quantity = %s WHERE id = %s;", (new_quantity, product_id))
    conn.commit()

    await interaction.response.send_message(f"Zaktualizowano zażądaną ilość produktu '{product_name}'.")

    await update_order_status_message(interaction.channel, order_id)

@bot.tree.command(name="zamówienie_usuń_produkt", description="Usuwa produkt z zamówienia w bieżącym wątku.")
@app_commands.autocomplete(product_name=product_name_autocomplete)
async def delete_product(interaction: discord.Interaction, product_name: str):
    thread_id = interaction.channel.id
    order_id = get_order_id_from_thread(thread_id)

    if not order_id:
        await interaction.response.send_message("Nie znaleziono zamówienia dla tego wątku.", ephemeral=True)
        return

    cursor.execute(
        "SELECT id FROM order_items WHERE order_id = %s AND product_name = %s;",
        (order_id, product_name)
    )
    if not cursor.fetchone():
        await interaction.response.send_message(f"Produkt '{product_name}' nie istnieje w tym zamówieniu.", ephemeral=True)
        return

    cursor.execute("DELETE FROM order_items WHERE order_id = %s AND product_name = %s;", (order_id, product_name))
    conn.commit()
    if product_name in product_names:
        product_names.remove(product_name)

    await update_order_status_message(interaction.channel, order_id)
    await interaction.response.send_message(f"Produkt '{product_name}' został usunięty z zamówienia.", ephemeral=True)


@bot.tree.command(name="zamówienie_pokaż", description="Wyświetla szczegóły zamówienia w bieżącym wątku.")
async def show_order(interaction: discord.Interaction):
    thread_id = interaction.channel.id
    order_id = get_order_id_from_thread(thread_id)

    if not order_id:
        await interaction.response.send_message("Nie znaleziono zamówienia dla tego wątku.", ephemeral=True)
        return

    order_details = await get_order_details(order_id)
    await interaction.response.send_message(f"```{order_details}```", ephemeral=True)


@bot.tree.command(name="zamówienie_zamknij", description="Oznacza zamówienie w bieżącym wątku jako zakończone i usuwa je z bazy danych.")
async def complete_order(interaction: discord.Interaction):
    thread_id = interaction.channel.id
    order_id = get_order_id_from_thread(thread_id)

    if not order_id:
        await interaction.response.send_message("Nie znaleziono zamówienia dla tego wątku.", ephemeral=True)
        return

    cursor.execute("SELECT completed, creator_id FROM orders WHERE id = %s;", (order_id,))
    order = cursor.fetchone()

    if not order:
        await interaction.response.send_message("Nie znaleziono zamówienia dla tego wątku.", ephemeral=True)
        return

    completed, creator_id = order

    if interaction.user.id != creator_id:
        await interaction.response.send_message("Tylko twórca zamówienia może je zamknąć.", ephemeral=True)
        return

    if completed:
        await interaction.response.send_message("Zamówienie jest już zakończone.", ephemeral=True)
        return

    await interaction.response.send_message("Zamówienie zostało oznaczone jako zakończone.", ephemeral=False)


@bot.event
async def on_ready():
    print(f"Bot gotowy! Zalogowano jako {bot.user}")



token = ""
bot.run(token)
