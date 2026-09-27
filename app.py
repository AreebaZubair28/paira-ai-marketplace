from flask import Flask, render_template, request, redirect, session, jsonify
import os
import re
from difflib import SequenceMatcher, get_close_matches
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

app.secret_key = os.getenv("FLASK_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY is missing from .env")

UPLOAD_FOLDER = 'static/uploads'

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'jfif', 'bmp'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_con():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        sslmode="require"
    )

def text_similarity(a, b):
    """Return a 0..1 similarity score for lightweight typo tolerance."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def fuzzy_word_match(word, text, cutoff=0.72):
    """Match a query word against text while tolerating small spelling mistakes."""
    word = word.lower().strip()
    tokens = re.findall(r"[a-z0-9]+", text.lower())

    if not word or not tokens:
        return False

    if word in text.lower():
        return True

    def one_edit_away(a, b):
        """True when two words differ by at most one insert/delete/replace."""
        if abs(len(a) - len(b)) > 1:
            return False

        if len(a) == len(b):
            return sum(x != y for x, y in zip(a, b)) <= 1

        if len(a) > len(b):
            a, b = b, a

        i = j = differences = 0
        while i < len(a) and j < len(b):
            if a[i] == b[j]:
                i += 1
                j += 1
            else:
                differences += 1
                if differences > 1:
                    return False
                j += 1

        return True

    for token in tokens:
        if len(word) >= 3 and len(token) >= 3 and one_edit_away(word, token):
            return True
        if text_similarity(word, token) >= cutoff:
            return True

    return False

def fuzzy_category(message, categories):
    """Find a category mentioned in a message, including close misspellings."""
    msg_words = re.findall(r"[a-z0-9]+", message.lower())
    aliases = {
        'bag': 'bags', 'bags': 'bags', 'handbag': 'bags', 'handbags': 'bags',
        'purse': 'bags', 'purses': 'bags', 'tote': 'bags', 'totes': 'bags',
        'shoe': 'shoes', 'shoes': 'shoes', 'sneaker': 'shoes', 'sneakers': 'shoes',
        'sandal': 'shoes', 'sandals': 'shoes', 'heel': 'shoes', 'heels': 'shoes',
        'footwear': 'shoes'
    }
    available = {c.lower(): c.lower() for c in categories}

    for word in msg_words:
        if word in aliases and aliases[word] in available:
            return aliases[word]
        if word in available:
            return word

    candidates = list(available) + [a for a in aliases if aliases[a] in available]
    for word in msg_words:
        close = get_close_matches(word, candidates, n=1, cutoff=0.72)
        if close:
            found = close[0]
            return aliases.get(found, found)
    return None

def role_home():
    """Return the appropriate landing page for the current logged-in role."""
    role = session.get("role")
    if role == "Admin":
        return "/admin"
    if role == "Seller":
        return "/seller"
    return "/products"

def customer_only():
    """Return True only for an authenticated customer account."""
    return "user_id" in session and session.get("role") == "Customer"

class ShoppingEnvironment:
    """
    Represents the marketplace environment.
    Products are percepts the agent observes.
    """

    def __init__(self, products, categories):
        self.products   = products
        self.categories = categories

    def get_percept(self, user_query):
        """Return current state of environment as percept"""
        return {
            'query':      user_query,
            'products':   self.products,
            'categories': self.categories
        }

class GoalBasedShoppingAgent:
    """ Goal-Based Agent for the Paira shopping chatbot."""

    def __init__(self, environment):
        self.env  = environment
        self.goal = None

    def set_goal(self, goal_type, params=None):
        """Agent sets its goal based on parsed user intent"""
        self.goal = {'type': goal_type, 'params': params or {}}

    def act(self, percept):
        """
        Agent selects action based on current goal.
        Returns list of matching products.
        """
        products = percept['products']
        params   = self.goal['params'] if self.goal else {}
        goal     = self.goal['type']   if self.goal else 'show_all'

        if goal == 'find_under_price':
            limit    = params.get('price', 99999)
            category = params.get('category')
            filtered = [p for p in products if float(p[2]) <= limit]

            if category:
                filtered = [p for p in filtered if category in p[4].lower()]

            return sorted(filtered, key=lambda x: x[2])

        elif goal == 'find_by_category':
            category = params.get('category', '')
            return [p for p in products if category in p[4].lower()]

        elif goal == 'find_cheapest':
            category = params.get('category')
            filtered = sorted(products, key=lambda x: x[2])

            if category:
                filtered = [p for p in filtered if category in p[4].lower()]
            return filtered

        elif goal == 'find_expensive':
            category = params.get('category')
            filtered = sorted(products, key=lambda x: x[2], reverse=True)

            if category:
                filtered = [p for p in filtered if category in p[4].lower()]
            return filtered

        elif goal == 'find_top_rated':
            category = params.get('category')
            filtered = sorted(products, key=lambda x: x[5], reverse=True)

            if category:
                filtered = [p for p in filtered if category in p[4].lower()]
            return filtered

        elif goal == 'find_above_price':
            minimum  = params.get('price', 0)
            category = params.get('category')
            filtered = [p for p in products if float(p[2]) >= minimum]

            if category:
                filtered = [p for p in filtered if category in p[4].lower()]
            return filtered

        elif goal == 'find_in_stock':
            return [p for p in products if p[3] > 0]

        elif goal == 'find_by_name':
            keywords = params.get('keywords', [])
            matched  = []

            for p in products:
                searchable = f"{p[1]} {p[4]}".lower()
                for w in keywords:
                    if w in searchable or fuzzy_word_match(w, searchable, cutoff=0.72):
                        matched.append(p)
                        break
            return matched

        else:
            return products

def greedy_best_first_search(products, query, max_price=None, min_price=None, category=None):
    """
    Informed Search using Greedy Best-First strategy.
    Heuristic = relevance score (higher = closer to goal).
    The agent always expands the node with highest heuristic value.
    """
    query_words = query.lower().split() if query else []

    def heuristic(product):
        """
        Heuristic function: estimates how relevant a product is.
        Higher score = better match to user's goal.
        """

        score = 0
        name  = product[1].lower()
        cat   = product[4].lower()
        price = float(product[2])
        rating = float(product[6]) if len(product) > 6 else 0

        searchable = f"{name} {cat}"
        for word in query_words:
            if word in name:
                score += 10
            elif fuzzy_word_match(word, name, cutoff=0.72):
                score += 8
            elif fuzzy_word_match(word, searchable, cutoff=0.72):
                score += 5

        if category and category.lower() in cat:
            score += 8

        if max_price and price > max_price:
            score -= 20

        elif max_price and price <= max_price:
            score += 5

        if min_price and price < min_price:
            score -= 10

        elif min_price and price >= min_price:
            score += 3

        score += rating * 2
        stock = product[3]

        if stock > 0:
            score += 3
        else:
            score -= 5

        return score

    ranked = sorted(products, key=heuristic, reverse=True)
    return ranked

def similarity_score(product, target_price, target_cat_id, target_product_id):
    """
    Computes similarity between a product and the target product.
    Uses feature-based similarity inspired by KNN concept:
      Feature 1: Category match (most important)
      Feature 2: Price proximity
      Feature 3: Rating
    Returns a score — higher = more similar.
    """

    if product[0] == target_product_id:
        return -1

    score = 0
    score += 50
    price_diff  = abs(float(product[2]) - float(target_price))
    price_score = max(0, 30 - (price_diff / 100))
    score      += price_score
    avg_rating = float(product[4])
    score     += avg_rating * 5
    if product[3] if len(product) > 3 else True:
        score += 2

    return score

@app.context_processor

def inject_cart_count():
    cart_count = 0

    if session.get('role') == 'Customer' and 'user_id' in session:
        try:
            con = get_con(); cur = con.cursor()
            cur.execute("""

                SELECT COALESCE(SUM(ci.quantity), 0)

                FROM CartItems ci

                JOIN Cart ca ON ci.cart_id = ca.cart_id

                WHERE ca.customer_id = %s

            """, (session['user_id'],))

            result = cur.fetchone()
            con.close()
            cart_count = int(result[0]) if result else 0
        except Exception:
            cart_count = 0

    return dict(cart_count=cart_count)

@app.route('/')

def home():
    category_id = request.args.get('category_id')
    con = get_con(); cur = con.cursor()

    if category_id:
        cur.execute("""
            SELECT p.product_id, p.name, p.price, p.stock,
                   c.category_name, u.name,
                   COALESCE((SELECT ROUND(AVG(r.rating),1) FROM Review r WHERE r.product_id = p.product_id), 0),
                   p.image_url
            FROM Product p
            JOIN Category c ON p.category_id = c.category_id
            JOIN Users    u ON p.seller_id   = u.user_id
            WHERE p.category_id = %s
            ORDER BY p.product_id DESC
        """, (category_id,))
    else:
        cur.execute("""
            SELECT p.product_id, p.name, p.price, p.stock,
                   c.category_name, u.name,
                   COALESCE((SELECT ROUND(AVG(r.rating),1) FROM Review r WHERE r.product_id = p.product_id), 0),
                   p.image_url
            FROM Product p
            JOIN Category c ON p.category_id = c.category_id
            JOIN Users    u ON p.seller_id   = u.user_id
            ORDER BY p.product_id DESC
        """)
    products = cur.fetchall()
    cur.execute("SELECT category_id, category_name FROM Category")
    categories = cur.fetchall()
    con.close()
    return render_template('home.html', products=products, categories=categories)

@app.route('/login', methods=['GET', 'POST'])

def login():
    if 'user_id' in session:
        if session.get('role') == 'Admin':  return redirect('/admin')
        if session.get('role') == 'Seller': return redirect('/seller')
        return redirect('/products')

    error = None

    if request.method == 'POST':
        email    = request.form['email']
        password = request.form['password']
        next_url = request.form.get('next') or request.args.get('next') or '/products'
        con = get_con(); cur = con.cursor()
        cur.execute(
            "SELECT user_id, name, role, password FROM Users WHERE email=%s",
            (email,)
        )

        user = cur.fetchone()

        password_ok = False
        if user:
            stored_password = user[3] or ""

            if stored_password.startswith(("scrypt:", "pbkdf2:")):
                password_ok = check_password_hash(stored_password, password)
            else:
                password_ok = stored_password == password
                if password_ok:
                    cur.execute(
                        "UPDATE Users SET password=%s WHERE user_id=%s",
                        (generate_password_hash(password), user[0])
                    )
                    con.commit()

        con.close()

        if user and password_ok:
            session['user_id'] = user[0]
            session['name']    = user[1]
            session['role']    = user[2]

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                if user[2] == 'Admin':  return jsonify({'redirect': '/admin'})
                if user[2] == 'Seller': return jsonify({'redirect': '/seller'})
                return jsonify({'redirect': next_url})

            if user[2] == 'Admin':  return redirect('/admin')
            if user[2] == 'Seller': return redirect('/seller')

            return redirect(next_url)

        error = "Invalid email or password."

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'error': error}), 401

    return render_template('login.html', error=error)

@app.route('/register', methods=['GET', 'POST'])

def register():
    if 'user_id' in session:
        if session.get('role') == 'Admin':  return redirect('/admin')
        if session.get('role') == 'Seller': return redirect('/seller')

        return redirect('/products')

    error = None

    if request.method == 'POST':
        name     = request.form['name']
        email    = request.form['email']
        password = request.form['password']
        role = request.form['role']
        if role not in {'Customer', 'Seller'}:
            return render_template('register.html', error='Invalid account type.'), 400

        con = get_con(); cur = con.cursor()
        cur.execute("SELECT user_id FROM Users WHERE email=%s", (email,))

        if cur.fetchone():
            error = "Email already registered."
            con.close()
        else:
            password_hash = generate_password_hash(password)
            cur.execute(
                "INSERT INTO Users (name, email, password, role) VALUES (%s,%s,%s,%s) RETURNING user_id",
                (name, email, password_hash, role)
            )
            new_user_id = cur.fetchone()[0]
            con.commit()
            con.close()

            session['user_id'] = new_user_id
            session['name'] = name
            session['role'] = role

            if role == 'Seller':
                return redirect('/seller')
            return redirect('/products')

    return render_template('register.html', error=error)

@app.route('/logout')

def logout():
    session.clear()
    return redirect('/')

@app.route('/products')

def show_products():
    if not customer_only():
        return redirect(role_home() if 'user_id' in session else '/login')

    category_id = request.args.get('category_id')
    con = get_con(); cur = con.cursor()

    if category_id:
        cur.execute("""
            SELECT p.product_id, p.name, p.price, p.stock,
                   c.category_name, u.name,
                   COALESCE((SELECT ROUND(AVG(r.rating),1) FROM Review r WHERE r.product_id = p.product_id), 0),
                   p.image_url
            FROM Product p
            JOIN Category c ON p.category_id = c.category_id
            JOIN Users    u ON p.seller_id   = u.user_id
            WHERE p.category_id = %s
            ORDER BY p.product_id
        """, (category_id,))
    else:
        cur.execute("""
            SELECT p.product_id, p.name, p.price, p.stock,
                   c.category_name, u.name,
                   COALESCE((SELECT ROUND(AVG(r.rating),1) FROM Review r WHERE r.product_id = p.product_id), 0),
                   p.image_url
            FROM Product p
            JOIN Category c ON p.category_id = c.category_id
            JOIN Users    u ON p.seller_id   = u.user_id
            ORDER BY p.product_id
        """)

    products = cur.fetchall()
    cur.execute("SELECT category_id, category_name FROM Category")
    categories = cur.fetchall()
    con.close()

    return render_template('products.html', products=products, categories=categories)

@app.route('/product/<int:product_id>')

def product_detail(product_id):
    con = get_con(); cur = con.cursor()
    cur.execute("""
        SELECT p.product_id, p.name, p.description, p.image_url, p.price, p.stock,
               c.category_name, u.name AS seller,
               COALESCE(ROUND(AVG(r.rating),1), 0) AS avg_rating,
               COUNT(r.review_id) AS review_count
        FROM Product p
        JOIN Category c ON p.category_id = c.category_id
        JOIN Users    u ON p.seller_id   = u.user_id
        LEFT JOIN Review r ON p.product_id = r.product_id
        WHERE p.product_id = %s
        GROUP BY p.product_id, p.name, p.description, p.image_url, p.price, p.stock,
                 c.category_name, u.name
    """, (product_id,))

    product = cur.fetchone()

    if not product:
        con.close()
        return redirect(role_home() if 'user_id' in session else '/')

    cur.execute("""
        SELECT u.name, r.rating, r.review_text, r.review_date
        FROM Review r
        JOIN Users u ON r.user_id = u.user_id
        WHERE r.product_id = %s
        ORDER BY r.review_date DESC
    """, (product_id,))

    reviews = cur.fetchall()
    con.close()

    return render_template('product_detail.html', product=product, reviews=reviews,
                           is_customer=customer_only())

@app.route('/review/add/<int:product_id>', methods=['POST'])

def add_review(product_id):
    if not customer_only():
        return redirect(role_home() if "user_id" in session else "/login")

    rating = request.form['rating']
    review_text = request.form['review_text']
    con = get_con(); cur = con.cursor()

    cur.execute("""
        INSERT INTO Review (user_id, product_id, rating, review_text, review_date)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
    """, (session['user_id'], product_id, rating, review_text))

    con.commit()
    con.close()

    return redirect(f'/product/{product_id}')

@app.route('/add_to_cart/<int:product_id>')

def add_to_cart(product_id):
    if not customer_only():
        return redirect(role_home() if "user_id" in session else "/login")

    customer_id = session['user_id']
    con = get_con(); cur = con.cursor()
    cur.execute("SELECT cart_id FROM Cart WHERE customer_id=%s", (customer_id,))
    cart = cur.fetchone()

    if cart:
        cart_id = cart[0]
    else:
        cur.execute("INSERT INTO Cart (customer_id) VALUES (%s) RETURNING cart_id", (customer_id,))
        cart_id = cur.fetchone()[0]

    cur.execute(
        "SELECT quantity FROM CartItems WHERE cart_id=%s AND product_id=%s",
        (cart_id, product_id)
    )

    if cur.fetchone():
        cur.execute(
            "UPDATE CartItems SET quantity=quantity+1 WHERE cart_id=%s AND product_id=%s",
            (cart_id, product_id)
        )
    else:
        cur.execute("INSERT INTO CartItems VALUES (%s,%s,1)", (cart_id, product_id))

    con.commit()
    con.close()

    referrer = request.referrer

    if referrer:
        return redirect(referrer)

    return redirect('/products')

@app.route('/remove_from_cart/<int:product_id>')

def remove_from_cart(product_id):
    if not customer_only():
        return redirect(role_home() if "user_id" in session else "/login")

    con = get_con(); cur = con.cursor()
    cur.execute("SELECT cart_id FROM Cart WHERE customer_id=%s", (session['user_id'],))
    cart = cur.fetchone()

    if cart:
        cur.execute(
            "DELETE FROM CartItems WHERE cart_id=%s AND product_id=%s",
            (cart[0], product_id)
        )
        con.commit()
    con.close()

    return redirect('/cart')

@app.route('/increase_qty/<int:product_id>')

def increase_qty(product_id):
    if not customer_only():
        return redirect(role_home() if "user_id" in session else "/login")

    con = get_con(); cur = con.cursor()
    cur.execute("SELECT cart_id FROM Cart WHERE customer_id=%s", (session['user_id'],))
    cart = cur.fetchone()

    if cart:
        cur.execute("SELECT stock FROM Product WHERE product_id=%s", (product_id,))
        product = cur.fetchone()
        cur.execute(
            "SELECT quantity FROM CartItems WHERE cart_id=%s AND product_id=%s",
            (cart[0], product_id)
        )
        cart_item = cur.fetchone()

        if product and cart_item and cart_item[0] < product[0]:
            cur.execute(
                "UPDATE CartItems SET quantity=quantity+1 WHERE cart_id=%s AND product_id=%s",
                (cart[0], product_id)
            )
            con.commit()
    con.close()

    return redirect('/cart')

@app.route('/decrease_qty/<int:product_id>')

def decrease_qty(product_id):
    if not customer_only():
        return redirect(role_home() if "user_id" in session else "/login")

    con = get_con(); cur = con.cursor()
    cur.execute("SELECT cart_id FROM Cart WHERE customer_id=%s", (session['user_id'],))
    cart = cur.fetchone()

    if cart:
        cur.execute(
            "SELECT quantity FROM CartItems WHERE cart_id=%s AND product_id=%s",
            (cart[0], product_id)
        )
        cart_item = cur.fetchone()

        if cart_item:
            if cart_item[0] > 1:
                cur.execute(
                    "UPDATE CartItems SET quantity=quantity-1 WHERE cart_id=%s AND product_id=%s",
                    (cart[0], product_id)
                )
            else:
                cur.execute(
                    "DELETE FROM CartItems WHERE cart_id=%s AND product_id=%s",
                    (cart[0], product_id)
                )
            con.commit()
    con.close()

    return redirect('/cart')

@app.route('/cart')

def view_cart():
    if not customer_only():
        return redirect(role_home() if "user_id" in session else "/login")

    con = get_con(); cur = con.cursor()
    cur.execute("""
        SELECT p.product_id, p.name, ci.quantity, p.price,
               (ci.quantity * p.price) AS subtotal,
               p.image_url
        FROM CartItems ci
        JOIN Product p  ON ci.product_id = p.product_id
        JOIN Cart    ca ON ci.cart_id    = ca.cart_id
        WHERE ca.customer_id = %s
    """, (session['user_id'],))

    cart_items = cur.fetchall()
    con.close()
    total = sum(item[4] for item in cart_items) if cart_items else 0

    return render_template('cart.html', cart_items=cart_items, total=total)

@app.route('/checkout', methods=['POST'])

def checkout():
    if not customer_only():
        return redirect(role_home() if "user_id" in session else "/login")

    if session.get('role') != 'Customer':
        return redirect('/products')

    customer_id = session['user_id']
    con = get_con(); cur = con.cursor()

    try:
        cur.execute("""
            SELECT ci.product_id, ci.quantity, p.price, ca.cart_id, p.stock, p.name
            FROM CartItems ci
            JOIN Product p  ON ci.product_id = p.product_id
            JOIN Cart    ca ON ci.cart_id    = ca.cart_id
            WHERE ca.customer_id = %s
        """, (customer_id,))

        items = cur.fetchall()
        if not items:
            con.close()
            return render_template('cart.html', cart_items=[], total=0,
                                   error="Your cart is empty!")

        for item in items:
            if item[1] > item[4]:
                con.close()
                return render_template('cart.html', cart_items=[], total=0,

                                       error="Not enough stock for product.")

        total_amount = sum(item[1] * item[2] for item in items)
        cart_id      = items[0][3]

        cur.execute(
            "INSERT INTO Orders (customer_id, order_date, total_amount) VALUES (%s,CURRENT_TIMESTAMP,%s) RETURNING order_id",
            (customer_id, total_amount)
        )

        order_id = cur.fetchone()[0]

        for item in items:
            cur.execute(
                "INSERT INTO OrderDetails (order_id, product_id, quantity, unit_price, product_name) VALUES (%s,%s,%s,%s,%s)",
                (order_id, item[0], item[1], item[2], item[5])
            )

        cur.execute(
            "INSERT INTO Payment (order_id, amount, status, payment_method, payment_date) VALUES (%s,%s,'Paid','Cash',CURRENT_TIMESTAMP)",
            (order_id, total_amount)
        )

        cur.execute("DELETE FROM CartItems WHERE cart_id=%s", (cart_id,))
        con.commit()
        con.close()

        return render_template('order_success.html', order_id=order_id, total=total_amount)
    except Exception as e:
        con.rollback(); con.close()
        return f"Error: {e}"

@app.route('/orders')

def orders():
    if not customer_only():
        return redirect(role_home() if "user_id" in session else "/login")

    con = get_con(); cur = con.cursor()
    cur.execute("""
        SELECT o.order_id, o.order_date, o.total_amount, pay.status
        FROM Orders o
        JOIN Payment pay ON o.order_id = pay.order_id
        WHERE o.customer_id = %s
        AND EXISTS (
            SELECT 1 FROM OrderDetails od WHERE od.order_id = o.order_id
        )
        ORDER BY o.order_date DESC
    """, (session['user_id'],))
    orders_list = cur.fetchall()
    con.close()
    return render_template('orders.html', orders=orders_list)

@app.route('/orders/<int:order_id>')

def order_detail(order_id):
    if 'user_id' not in session:
        return redirect('/login')

    if session.get('role') == 'Seller':
        return redirect('/seller')

    con = get_con(); cur = con.cursor()

    if session.get('role') == 'Admin':
        cur.execute("""
            SELECT o.order_id, o.order_date, o.total_amount, pay.status
            FROM Orders o JOIN Payment pay ON o.order_id = pay.order_id
            WHERE o.order_id=%s
        """, (order_id,))
    else:
        cur.execute("""
            SELECT o.order_id, o.order_date, o.total_amount, pay.status
            FROM Orders o JOIN Payment pay ON o.order_id = pay.order_id
            WHERE o.order_id=%s AND o.customer_id=%s
        """, (order_id, session['user_id']))

    order = cur.fetchone()
    if not order:
        con.close()
        return redirect('/orders' if session.get('role') == 'Customer' else '/admin')

    cur.execute("""
        SELECT COALESCE(p.name, od.product_name) AS product_name,
               od.quantity, od.unit_price,
               (od.quantity * od.unit_price) AS subtotal
        FROM OrderDetails od
        LEFT JOIN Product p ON od.product_id = p.product_id
        WHERE od.order_id = %s
    """, (order_id,))

    items = cur.fetchall()
    con.close()
    return render_template('order_detail.html', order=order, items=items)

@app.route('/seller')

def seller_dashboard():
    if session.get('role') != 'Seller':
        return redirect('/login')

    uid = session['user_id']
    search_name     = request.args.get('search_name', '').strip()
    search_category = request.args.get('search_category', '').strip()
    con = get_con(); cur = con.cursor()
    cur.execute("""
        SELECT COUNT(DISTINCT p.product_id),
               COALESCE(SUM(od.quantity * od.unit_price), 0),
               COALESCE(ROUND(AVG(r.rating), 1), 0)
        FROM Product p
        LEFT JOIN OrderDetails od ON p.product_id = od.product_id
        LEFT JOIN Review        r ON p.product_id = r.product_id
        WHERE p.seller_id = %s
    """, (uid,))
    stats = cur.fetchone()
    sql = """
        SELECT p.product_id, p.name, p.price, p.stock, c.category_name,
               COALESCE((SELECT SUM(od.quantity) FROM OrderDetails od WHERE od.product_id = p.product_id), 0),
               p.image_url,
               COALESCE((SELECT ROUND(AVG(r.rating),1) FROM Review r WHERE r.product_id = p.product_id), 0),
               (SELECT COUNT(*) FROM Review r WHERE r.product_id = p.product_id)
        FROM Product p
        JOIN Category c ON p.category_id = c.category_id
        WHERE p.seller_id = %(seller_id)s
    """
    params = {'seller_id': uid}
    sql += " ORDER BY p.product_id"
    cur.execute(sql, params)
    products = cur.fetchall()

    if search_name:
        query_words = re.findall(r"[a-z0-9]+", search_name.lower())
        def seller_name_matches(product):
            searchable = (product[1] or '').lower()
            return all(
                (word in searchable) or fuzzy_word_match(word, searchable, cutoff=0.72)
                for word in query_words
            )
        products = [p for p in products if seller_name_matches(p)]

    if search_category:
        category_words = re.findall(r"[a-z0-9]+", search_category.lower())
        def seller_category_matches(product):
            searchable = (product[4] or '').lower()
            return all(
                (word in searchable) or fuzzy_word_match(word, searchable, cutoff=0.72)
                for word in category_words
            )
        products = [p for p in products if seller_category_matches(p)]

    cur.execute("SELECT category_id, category_name FROM Category ORDER BY category_name")
    categories = cur.fetchall()
    cur.execute("""
        SELECT p.name, u.name AS customer, r.rating, r.review_text, r.review_date
        FROM Review r
        JOIN Product p ON r.product_id = p.product_id
        JOIN Users   u ON r.user_id    = u.user_id
        WHERE p.seller_id = %s
        ORDER BY r.review_date DESC
    """, (uid,))
    reviews = cur.fetchall()
    cur.execute("""
        SELECT
            o.order_id,
            o.order_date,
            u.name        AS buyer_name,
            p.name        AS product_name,
            od.quantity,
            od.unit_price,
            (od.quantity * od.unit_price) AS subtotal,
            pay.status
        FROM Orders o
        JOIN OrderDetails od  ON o.order_id    = od.order_id
        JOIN Product p        ON od.product_id = p.product_id
        JOIN Users u          ON o.customer_id = u.user_id
        LEFT JOIN Payment pay ON o.order_id    = pay.order_id
        WHERE p.seller_id = %s
        ORDER BY o.order_date DESC
    """, (uid,))
    seller_orders = cur.fetchall()
    con.close()

    return render_template('seller_dashboard.html',
                           stats=stats, products=products, reviews=reviews,
                           categories=categories,
                           seller_orders=seller_orders,
                           search_name=search_name,
                           search_category=search_category)

@app.route('/add_product', methods=['GET', 'POST'])

def add_product():
    if session.get('role') != 'Seller':
        return redirect('/login')

    con = get_con(); cur = con.cursor()
    cur.execute("SELECT category_id, category_name FROM Category")
    categories = cur.fetchall()

    if request.method == 'POST':
        name        = request.form['name']
        description = request.form.get('description', '')
        price       = request.form['price']
        stock       = request.form['stock']
        category_id = request.form['category_id']
        image = request.files.get('image')
        image_url = ''

        if image and image.filename != '' and allowed_file(image.filename):
            filename = secure_filename(image.filename)
            image.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            image_url = filename

        cur.execute("""
            INSERT INTO Product (name, description, price, stock, seller_id, category_id, image_url)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (name, description, price, stock, session['user_id'], category_id, image_url))

        con.commit(); con.close()
        return redirect('/seller')
    con.close()

    return render_template('add_product.html', categories=categories)

@app.route('/edit_product/<int:product_id>', methods=['GET', 'POST'])

def edit_product(product_id):
    if session.get('role') != 'Seller':
        return redirect('/login')

    con = get_con(); cur = con.cursor()

    if request.method == 'POST':
        image = request.files.get('image')
        remove_image = request.form.get('remove_image') == '1'

        if image and image.filename != '' and allowed_file(image.filename):
            filename = secure_filename(image.filename)
            image.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            image_url = filename

            cur.execute(
                "UPDATE Product SET name=%s, description=%s, price=%s, stock=%s, category_id=%s, image_url=%s WHERE product_id=%s AND seller_id=%s",
                (request.form['name'], request.form['description'],
                 request.form['price'], request.form['stock'],
                 request.form['category_id'], image_url, product_id, session['user_id'])
            )
        elif remove_image:
            cur.execute(
                "UPDATE Product SET name=%s, description=%s, price=%s, stock=%s, category_id=%s, image_url='' WHERE product_id=%s AND seller_id=%s",
                (request.form['name'], request.form['description'],
                 request.form['price'], request.form['stock'],
                 request.form['category_id'], product_id, session['user_id'])
            )
        else:
            cur.execute(
                "UPDATE Product SET name=%s, description=%s, price=%s, stock=%s, category_id=%s WHERE product_id=%s AND seller_id=%s",
                (request.form['name'], request.form['description'],
                 request.form['price'], request.form['stock'],
                 request.form['category_id'], product_id, session['user_id'])
            )
        con.commit(); con.close()
        return redirect('/seller')

    cur.execute(
        "SELECT product_id, name, price, stock, description, category_id, image_url FROM Product WHERE product_id=%s AND seller_id=%s",
        (product_id, session['user_id'])
    )

    product = cur.fetchone()
    cur.execute("SELECT category_id, category_name FROM Category")
    categories = cur.fetchall()
    con.close()

    return render_template('edit_product.html', product=product, categories=categories)

@app.route('/delete_product/<int:product_id>')
def delete_product(product_id):
    if session.get('role') != 'Seller':
        return redirect('/login')

    con = get_con()
    cur = con.cursor()

    try:
        cur.execute(
            "SELECT name FROM Product WHERE product_id=%s AND seller_id=%s",
            (product_id, session['user_id'])
        )
        product = cur.fetchone()

        if not product:
            con.close()
            return redirect('/seller')

        product_name = product[0]

        cur.execute(
            "UPDATE OrderDetails SET product_name=%s WHERE product_id=%s",
            (product_name, product_id)
        )
        cur.execute("DELETE FROM CartItems WHERE product_id=%s", (product_id,))
        cur.execute("UPDATE OrderDetails SET product_id=NULL WHERE product_id=%s", (product_id,))
        cur.execute(
            "DELETE FROM Product WHERE product_id=%s AND seller_id=%s",
            (product_id, session['user_id'])
        )

        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    return redirect('/seller')

@app.route('/admin')

def admin_dashboard():
    if session.get('role') != 'Admin':
        return redirect('/login')

    con = get_con(); cur = con.cursor()
    cur.execute("SELECT user_id, name, email, role FROM Users ORDER BY role")
    raw_users = cur.fetchall()
    users = [(int(row[0]), row[1], row[2], row[3]) for row in raw_users]
    cur.execute("""
        SELECT
            p.product_id,
            p.name,
            COALESCE(p.description, ''),
            p.price,
            p.stock,
            c.category_name,
            u.name AS seller_name,
            u.email AS seller_email,
            COALESCE(p.image_url, ''),
            COALESCE(ROUND(
                (SELECT AVG(r.rating) FROM Review r WHERE r.product_id = p.product_id)
            , 1), 0) AS avg_rating,
            (SELECT COUNT(*) FROM Review r WHERE r.product_id = p.product_id) AS review_count,
            COALESCE(
                (SELECT SUM(od.quantity) FROM OrderDetails od WHERE od.product_id = p.product_id)
            , 0) AS units_sold
        FROM Product p
        JOIN Category c ON p.category_id = c.category_id
        JOIN Users    u ON p.seller_id   = u.user_id
        ORDER BY p.product_id
    """)

    raw_products = cur.fetchall()
    products = [
        (
            int(row[0]),
            row[1],
            row[2],
            float(row[3]),
            int(row[4]),
            row[5],
            row[6],
            row[7],
            row[8],
            float(row[9]),
            int(row[10]),
            int(row[11]),
        )
        for row in raw_products
    ]

    cur.execute("""
        SELECT
            o.order_id,
            o.customer_id,
            u.name,
            o.order_date,
            o.total_amount,
            pay.status,
            STRING_AGG(
                od.product_name || ' (Qty: ' || od.quantity || ')',
                ', ' ORDER BY od.od_id
            ) AS items
        FROM Orders o
        JOIN Users   u   ON o.customer_id = u.user_id
        JOIN Payment pay ON o.order_id    = pay.order_id
        LEFT JOIN OrderDetails od ON o.order_id = od.order_id
        GROUP BY o.order_id, o.customer_id, u.name, o.order_date, o.total_amount, pay.status
        ORDER BY o.order_date DESC
    """)
    raw_orders = cur.fetchall()
    orders = [
        (int(row[0]), int(row[1]), row[2], row[3], float(row[4]), row[5], row[6])
        for row in raw_orders
    ]

    cur.execute("""
        SELECT TO_CHAR(o.order_date, 'DD Mon') AS day,
               COALESCE(SUM(o.total_amount), 0),
               COUNT(o.order_id)
        FROM Orders o
        WHERE o.order_date >= CURRENT_TIMESTAMP - INTERVAL '7 days'
        GROUP BY TO_CHAR(o.order_date, 'DD Mon'), DATE(o.order_date)
        ORDER BY DATE(o.order_date)
    """)

    chart_rows  = cur.fetchall()
    dates       = [r[0] for r in chart_rows]
    sales_data  = [float(r[1]) for r in chart_rows]
    orders_count = [int(r[2]) for r in chart_rows]

    con.close()

    return render_template('admin_dashboard.html',
                           users=users,
                           products=products,
                           orders=orders,
                           dates=dates,
                           sales_data=sales_data,
                           orders_count=orders_count)

@app.route('/search')

def search():
    if session.get("role") in ("Admin", "Seller"):
        return redirect(role_home())

    query       = request.args.get('q', '').strip()
    category_id = request.args.get('category_id', '').strip()
    con = get_con(); cur = con.cursor()

    if category_id:
        cur.execute("""
            SELECT p.product_id, p.name, p.price, p.stock,
                   c.category_name, u.name,
                   COALESCE((SELECT ROUND(AVG(r.rating),1) FROM Review r WHERE r.product_id = p.product_id), 0),
                   p.image_url, COALESCE(p.description, '')
            FROM Product p
            JOIN Category c ON p.category_id = c.category_id
            JOIN Users    u ON p.seller_id   = u.user_id
            WHERE p.category_id = %s
            ORDER BY p.product_id DESC
        """, (category_id,))
    elif query:
        cur.execute("""
            SELECT p.product_id, p.name, p.price, p.stock,
                   c.category_name, u.name,
                   COALESCE((SELECT ROUND(AVG(r.rating),1) FROM Review r WHERE r.product_id = p.product_id), 0),
                   p.image_url, COALESCE(p.description, '')
            FROM Product p
            JOIN Category c ON p.category_id = c.category_id
            JOIN Users    u ON p.seller_id   = u.user_id
            ORDER BY p.product_id DESC
        """)
    else:
        return redirect('/')

    all_products = cur.fetchall()
    cur.execute("SELECT category_id, category_name FROM Category")
    categories = cur.fetchall()
    con.close()

    category_name = None

    if category_id:
        for cat in categories:
            if str(cat[0]) == str(category_id):
                category_name = cat[1]
                break

    products = greedy_best_first_search(
        all_products,
        query = query,
        category = category_name
    )

    if query and not category_id:
        q = query.lower()
        q_words = q.split()

        def matches(p):
            searchable = ' '.join([
                p[1] or '',
                p[4] or '',
                p[5] or '',
                p[8] or ''
            ]).lower()

            if q in searchable:
                return True

            return all(
                (w in searchable) or fuzzy_word_match(w, searchable, cutoff=0.72)
                for w in q_words
            )
        products = [p for p in products if matches(p)]

    if session.get('role') == 'Customer':
        return render_template('products.html',
                               products=products,
                               categories=categories,
                               search_query=query)

    return render_template('home.html',
                           products=products,
                           categories=categories,
                           search_query=query)

@app.route('/chatbot', methods=['POST'])

def chatbot():
    if session.get('role') in ('Admin', 'Seller'):
        return jsonify({"reply": "Shopping assistant is available on the customer storefront."}), 403

    data    = request.get_json(silent=True) or {}
    message = data.get('message', '').strip().lower()

    if not message:
        return jsonify({'reply': 'Please type a message.'})

    con = get_con(); cur = con.cursor()
    cur.execute("""
        SELECT p.product_id, p.name, p.price, p.stock,
               c.category_name,
               COALESCE(ROUND(AVG(r.rating),1),0) AS avg_rating
        FROM Product p
        JOIN Category c ON p.category_id = c.category_id
        LEFT JOIN Review r ON r.product_id = p.product_id
        GROUP BY p.product_id, p.name, p.price, p.stock, c.category_name
        ORDER BY p.product_id
    """)
    rows = cur.fetchall()
    cur.execute("SELECT category_name FROM Category")
    all_cats = [r[0].lower() for r in cur.fetchall()]
    con.close()

    env   = ShoppingEnvironment(rows, all_cats)
    agent = GoalBasedShoppingAgent(env)

    if any(w in message for w in ['hi', 'hello', 'hey', 'salam', 'assalam', 'good morning', 'good evening']):
        return jsonify({'reply': "Hi! 👋 Welcome to Paira! I can help you find bags, shoes, and accessories. What are you looking for?"})

    if 'help' in message or 'what can you do' in message:
        return jsonify({'reply': (
            "I can help you with:\n"
            "• Find products by name or category\n"
            "• Show cheapest or most expensive items\n"
            "• Check stock availability\n"
            "• Show top rated products\n\n"
            "Just ask me anything!"
        )})

    category_param = fuzzy_category(message, all_cats)
    categories_found = {category_param} if category_param else set()

    price_limit = None
    price_min = None

    under_match = re.search(
        r"(?:under|below|less than|cheaper than|max(?:imum)?)\s*(?:rs\.?\s*)?([0-9][0-9,]*)",
        message
    )
    if under_match:
        price_limit = int(under_match.group(1).replace(',', ''))

    above_match = re.search(
        r"(?:above|over|more than|greater than|at\s*least|minimum)\s*(?:rs\.?\s*)?([0-9][0-9,]*)",
        message
    )
    if above_match:
        price_min = int(above_match.group(1).replace(',', ''))

    if any(w in message for w in ['stock', 'available', 'in stock', 'availability']):
        agent.set_goal('find_in_stock')

    elif any(w in message for w in ['cheapest', 'cheap', 'lowest price', 'affordable', 'budget']):
        agent.set_goal('find_cheapest', {'category': category_param})

    elif any(w in message for w in ['expensive', 'premium', 'luxury', 'highest price']):
        agent.set_goal('find_expensive', {'category': category_param})

    elif any(w in message for w in ['best', 'top rated', 'top-rated', 'rating', 'popular', 'recommended']):
        agent.set_goal('find_top_rated', {'category': category_param})

    elif price_limit:
        agent.set_goal('find_under_price', {'price': price_limit, 'category': category_param})

    elif price_min:
        agent.set_goal('find_above_price', {'price': price_min, 'category': category_param})

    elif categories_found:
        agent.set_goal('find_by_category', {'category': category_param})

    elif any(w in message for w in ['everything', 'show all', 'list all', 'all products']):
        agent.set_goal('show_all')

    else:
        stop_words = {
            'the', 'and', 'for', 'with', 'you', 'have', 'show', 'find', 'want',
            'need', 'looking', 'please', 'some', 'any', 'product', 'products',
            'give', 'tell', 'about', 'can', 'could', 'would', 'what', 'which'
        }
        keywords = [
            w for w in re.findall(r"[a-z0-9]+", message)
            if len(w) > 2 and w not in stop_words
        ]
        agent.set_goal('find_by_name', {'keywords': keywords})

    percept = env.get_percept(message)
    results = agent.act(percept)

    if not results:
        return jsonify({'reply': "Sorry, I couldn't find any matching products. Try a different search!"})

    goal_type = agent.goal['type']

    if goal_type == 'find_in_stock':
        in_stock  = [r for r in results if r[3] > 0]
        out_stock = [r for r in rows if r[3] == 0]
        reply = f"📦 We have {len(in_stock)} products in stock and {len(out_stock)} out of stock.\n\nIn stock:\n"

        for p in in_stock[:5]:
            reply += f"• {p[1]} — Rs {p[2]} (Stock: {p[3]}) [product_id:{p[0]}]\n"
    elif goal_type == 'find_under_price':
        reply = f"🛍️ Products under Rs {price_limit}:\n"

        for p in results[:6]:
            reply += f"• {p[1]} — Rs {p[2]} | {p[4]} [product_id:{p[0]}]\n"

        if not results:
            reply = f"Sorry, no products found under Rs {price_limit}. Try a higher budget!"
    elif goal_type == 'find_above_price':
        reply = f"🛍️ Products above Rs {price_min}:\n"

        for p in results[:6]:
            reply += f"• {p[1]} — Rs {p[2]} | {p[4]} [product_id:{p[0]}]\n"
    elif goal_type == 'find_cheapest':
        reply = "💰 Most affordable products:\n"

        for p in results[:5]:
            reply += f"• {p[1]} — Rs {p[2]} | {p[4]} [product_id:{p[0]}]\n"
    elif goal_type == 'find_expensive':
        reply = "✨ Premium products:\n"

        for p in results[:5]:
            reply += f"• {p[1]} — Rs {p[2]} | {p[4]} [product_id:{p[0]}]\n"
    elif goal_type == 'find_top_rated':
        reply = "⭐ Top rated products:\n"

        for p in results[:5]:
            reply += f"• {p[1]} — Rs {p[2]} | ⭐{p[5]} [product_id:{p[0]}]\n"
    elif goal_type == 'find_by_category':
        label = category_param.title() if category_param else 'Products'
        reply = f"🛍️ Our {label} collection ({len(results)} items):\n"

        for p in results[:6]:
            reply += f"• {p[1]} — Rs {p[2]} | ⭐{p[5]} [product_id:{p[0]}]\n"
    elif goal_type == 'find_by_name':
        if results:
            reply = "🔍 Here's what I found:\n"
            for p in results[:6]:
                reply += f"• {p[1]} — Rs {p[2]} | {p[4]} | ⭐{p[5]} [product_id:{p[0]}]\n"
        else:
            return jsonify({'reply': (
                "I'm not sure about that. Try asking:\n"
                "• 'Show me bags under Rs 3000'\n"
                "• 'Cheapest shoes'\n"
                "• 'Top rated products'\n"
                "• 'What bags do you have?'"
            )})
    else:
        reply = "🛍️ All our products:\n"
        for p in results[:8]:
            reply += f"• {p[1]} — Rs {p[2]} | {p[4]} [product_id:{p[0]}]\n"

    return jsonify({'reply': reply.strip()})

@app.route('/recommendations/<int:product_id>')

def recommendations(product_id):
    if session.get('role') in ('Admin', 'Seller'):
        return jsonify([]), 403

    con = get_con(); cur = con.cursor()
    cur.execute("SELECT category_id, price FROM Product WHERE product_id=%s", (product_id,))
    row = cur.fetchone()

    if not row:
        con.close()
        return jsonify([])

    cat_id, price = row

    cur.execute("""
        SELECT p.product_id, p.name, p.price, p.image_url,
                   COALESCE(ROUND(AVG(r.rating),1),0) AS avg_rating,
                   c.category_name
            FROM Product p
            JOIN Category c ON p.category_id = c.category_id
            LEFT JOIN Review r ON r.product_id = p.product_id
            WHERE p.category_id = %s
              AND p.product_id  != %s
              AND p.stock > 0
            GROUP BY p.product_id, p.name, p.price, p.image_url, c.category_name
            ORDER BY avg_rating DESC
        LIMIT 20
    """, (cat_id, product_id))

    candidates = cur.fetchall()

    if len(candidates) < 3:
        cur.execute("""
            SELECT p.product_id, p.name, p.price, p.image_url,
                       COALESCE(ROUND(AVG(r.rating),1),0) AS avg_rating,
                       c.category_name
                FROM Product p
                JOIN Category c ON p.category_id = c.category_id
                LEFT JOIN Review r ON r.product_id = p.product_id
                WHERE p.product_id != %s
                  AND p.stock > 0
                GROUP BY p.product_id, p.name, p.price, p.image_url, c.category_name
                ORDER BY avg_rating DESC
            LIMIT 20
        """, (product_id,))

        candidates = cur.fetchall()

    con.close()

    scored = []

    for c in candidates:
        score = similarity_score(c, float(price), cat_id, product_id)
        price_diff = abs(float(c[2]) - float(price))

        if price_diff <= float(price) * 0.5:
            score += 10

        scored.append((score, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    recs = [item[1] for item in scored[:6]]
    result = [
        {
            "product_id":    r[0],
            "name":          r[1],
            "price":         float(r[2]),
            "image_url":     r[3] or "",
            "avg_rating":    float(r[4]),
            "category_name": r[5]
        }
        for r in recs
    ]

    return jsonify(result)

if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1")
