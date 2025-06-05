from unittest import result
from flask import Flask, render_template, jsonify, request, send_file, url_for, Response, redirect, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, BooleanField
from wtforms.validators import DataRequired, Length, Email, EqualTo, ValidationError
import os
import requests
import json
import re
from urllib.parse import urljoin, urlparse, quote as url_quote
import datetime
import subprocess
import tempfile
import shutil
from werkzeug.utils import secure_filename
# Removed: import sys (no longer needed without getattr(sys, 'frozen'))
# Removed: from pathlib import Path (no longer strictly needed without BUNDLE_DIR)
# Removed: import atexit, time (no longer needed for subprocess management)


app = Flask(__name__) # Reverted: No template_folder/static_folder arguments needed for standard deployment


# --- Configurations ---
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'SUPER_SECRET_DEV_KEY_123!@#_CHANGE_THIS_IMMEDIATELY')
basedir = os.path.abspath(os.path.dirname(__file__))
# Database path for cloud deployment. Will be relative to app's root on server.
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'bingewatch.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login_page'
login_manager.login_message_category = 'info'

# --- User Model ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(30), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    watch_history = db.relationship('WatchHistory', backref='user', lazy='dynamic', cascade="all, delete-orphan")
    def set_password(self, password): self.password_hash = generate_password_hash(password)
    def check_password(self, password): return check_password_hash(self.password_hash, password)
    def __repr__(self): return f"<User {self.username}>"

class WatchHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    anime_id = db.Column(db.String, nullable=False); anime_title_romaji = db.Column(db.String(255), nullable=True)
    anime_cover_image = db.Column(db.String(500), nullable=True); consumet_episode_id = db.Column(db.String(255), nullable=False)
    episode_number_str = db.Column(db.String(30), nullable=True)
    watched_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    progress_seconds = db.Column(db.Float, default=0.0); total_duration_seconds = db.Column(db.Float, default=0.0)
    is_complete = db.Column(db.Boolean, default=False)
    __table_args__ = (db.UniqueConstraint('user_id', 'consumet_episode_id', name='_user_episode_uc'),)
    def __repr__(self): return f"<WatchHistory User {self.user_id} - Anime {self.anime_id} - Ep {self.episode_number_str}>"

@login_manager.user_loader
def load_user(user_id): return User.query.get(int(user_id))

class RegistrationForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=30)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6)])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired(), EqualTo('password', message='Passwords must match.')])
    submit = SubmitField('Sign Up')
    def validate_username(self, username):
        if User.query.filter_by(username=username.data).first(): raise ValidationError('Username taken.')
    def validate_email(self, email):
        if User.query.filter_by(email=email.data).first(): raise ValidationError('Email in use.')
class LoginForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()]); password = PasswordField('Password', validators=[DataRequired()])
    remember = BooleanField('Remember Me'); submit = SubmitField('Login')

# CONUMET_API_URL now reads from environment variable, defaults to localhost for local dev
CONSUMET_API_URL = os.environ.get('CONSUMET_API_BASE_URL', "http://localhost:3000")
ANILIST_API_URL = "https://graphql.anilist.co"
DEFAULT_PROVIDER = "zoro" # Not changed, still "zoro"


def get_current_season_and_year():
    now = datetime.datetime.now(datetime.timezone.utc); month = now.month; year = now.year
    if 1 <= month <= 3: season = "WINTER"
    elif 4 <= month <= 6: season = "SPRING"
    elif 7 <= month <= 9: season = "SUMMER"
    else: season = "FALL"
    return season, year
def get_base_title(title_str):
    if not title_str: return ""
    t = title_str.lower()
    common_tags = [ r'\s*\(\s*(dub|sub|english dub|eng dub|tv|movie|ova|ona|special|uncensored|official|s\d+|season\s*\d+|\d{4})\s*\)', r'\s*\[\s*(bd|blu-ray)\s*\]',]
    for tag_regex in common_tags: t = re.sub(tag_regex, '', t, flags=re.IGNORECASE)
    trailing_phrases = [" english dub", " eng dub", " dub", " sub"];
    for phrase in trailing_phrases:
        if t.endswith(phrase.lower()): t = t[:-len(phrase)]
    if t.startswith("the "): t = t[4:]
    t = re.sub(r'[^a-z0-9\s]', '', t); t = re.sub(r'\s+', ' ', t).strip()
    return t

def find_zoro_anime_id_from_anilist_titles(anilist_title_romaji, anilist_title_english):
    norm_anilist_romaji = get_base_title(anilist_title_romaji)
    norm_anilist_english = get_base_title(anilist_title_english) if anilist_title_english else None

    search_queries_ordered = []
    if anilist_title_romaji:
        search_queries_ordered.append(anilist_title_romaji)
        search_queries_ordered.append(f"{anilist_title_romaji} (Dub)")
        search_queries_ordered.append(f"{anilist_title_romaji} (Sub)")
    if anilist_title_english and anilist_title_english.lower() != (anilist_title_romaji or "").lower():
        search_queries_ordered.append(anilist_title_english)
        search_queries_ordered.append(f"{anilist_title_english} (Dub)")
        search_queries_ordered.append(f"{anilist_title_english} (Sub)")

    if norm_anilist_romaji and norm_anilist_romaji not in search_queries_ordered:
        search_queries_ordered.append(norm_anilist_romaji)
    if norm_anilist_english and norm_anilist_english not in search_queries_ordered and norm_anilist_english != norm_anilist_romaji:
        search_queries_ordered.append(norm_anilist_english)

    if not search_queries_ordered and anilist_title_romaji:
        first_word = anilist_title_romaji.split(" ")[0]
        if first_word:
            search_queries_ordered.append(first_word)

    unique_search_queries = []
    seen = set()
    for q in search_queries_ordered:
        if q and q not in seen: 
            unique_search_queries.append(q)
            seen.add(q)

    best_match = {"id": None, "score": 0, "title": "", "subOrDub": ""}

    for query in unique_search_queries:
        sanitized_query = url_quote(query.replace("/", " "))
        zoro_search_url = f"{CONSUMET_API_URL}/anime/zoro/{sanitized_query}"
        try:
            res = requests.get(zoro_search_url, timeout=15)
            if res.status_code == 404:
                continue
            res.raise_for_status()
            search_data = res.json()

            if search_data.get("results"):
                for result in search_data["results"]:
                    zoro_result_title = result.get("title", "")
                    norm_zoro_title = get_base_title(zoro_result_title)
                    result_sub_or_dub = result.get("subOrDub", "sub").lower() 

                    current_score = 0
                    if (norm_zoro_title == norm_anilist_romaji) or \
                       (norm_anilist_english and norm_zoro_title == norm_anilist_english):
                        current_score = 3
                    elif (norm_anilist_romaji and norm_anilist_romaji in norm_zoro_title) or \
                         (norm_anilist_english and norm_anilist_english in norm_zoro_title):
                        current_score = 2
                    elif (norm_zoro_title and norm_anilist_romaji and norm_zoro_title in norm_anilist_romaji) or \
                         (norm_zoro_title and norm_anilist_english and norm_anilist_english and norm_zoro_title in norm_anilist_english):
                        current_score = 1

                    if current_score > best_match["score"]:
                        best_match = {"id": result["id"], "score": current_score, "title": zoro_result_title, "subOrDub": result_sub_or_dub}
                    elif current_score == best_match["score"] and current_score > 0:
                        if len(norm_zoro_title) < len(get_base_title(best_match["title"])):
                            best_match = {"id": result["id"], "score": current_score, "title": zoro_result_title, "subOrDub": result_sub_or_dub}
        except Exception as e:
            print(f"Error during Zoro ANIME ID search with query '{query}': {e}")

    if best_match["id"]:
        return best_match["id"]
    return None


def query_anilist(query, variables):
    try:
        response = requests.post(ANILIST_API_URL, json={'query': query, 'variables': variables}, headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        print(f"Anilist HTTP API error. Status: {e.response.status_code}. Query: [{query[:200]}...]. Variables: {json.dumps(variables)}. Response: {e.response.text[:500]}")
        return None
    except Exception as e:
        print(f"Anilist general API error. Query: [{query[:200]}...]. Variables: {json.dumps(variables)}. Error: {e}")
        return None

media_fields_for_list = "id bannerImage coverImage { large medium extraLarge } title { romaji english } episodes averageScore format nextAiringEpisode { episode }"

@app.route('/')
def index():
    return render_template('index.html', now=datetime.datetime.now(datetime.timezone.utc))

@app.route('/anime/<int:anime_id>')
def anime_details_page(anime_id):
    query_str = """query ($id: Int) { Media(id: $id, type: ANIME) { id title { romaji english native } description(asHtml: true) coverImage { extraLarge large medium color } bannerImage episodes duration status nextAiringEpisode { airingAt timeUntilAiring episode } startDate { year month day } endDate { year month day } season seasonYear format averageScore genres studios(isMain: true) { nodes { name } } } }"""
    variables = {'id': anime_id}
    data = query_anilist(query_str, variables)
    if data and data.get('data') and data['data'].get('Media'):
        anime_data = data['data']['Media']
        return render_template('anime_details.html', anime=anime_data, now=datetime.datetime.now(datetime.timezone.utc))
    return "Anime not found or API error", 404

@app.route('/genres')
def genres_page():
    return render_template('genres_page.html', now=datetime.datetime.now(datetime.timezone.utc))

@app.route('/register', methods=['GET', 'POST'])
def register_page():
    if current_user.is_authenticated: return redirect(url_for('index'))
    form = RegistrationForm()
    if form.validate_on_submit():
        user = User(username=form.username.data, email=form.email.data)
        user.set_password(form.password.data)
        db.session.add(user); db.session.commit()
        flash('Your account has been created! You can now log in.', 'success')
        return redirect(url_for('login_page'))
    return render_template('register.html', title='Register', form=form, now=datetime.datetime.now(datetime.timezone.utc))

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if current_user.is_authenticated: return redirect(url_for('index'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and user.check_password(form.password.data):
            login_user(user, remember=form.remember.data)
            next_page = request.args.get('next')
            flash('Login successful!', 'success')
            return redirect(next_page) if next_page else redirect(url_for('index'))
        else:
            flash('Login Unsuccessful. Please check email and password.', 'danger')
    return render_template('login.html', title='Login', form=form, now=datetime.datetime.now(datetime.timezone.utc))

@app.route('/logout')
@login_required
def logout_page():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))

def parse_episode_number(ep_num_str):
    if not ep_num_str:
        return 0.0
    match = re.search(r'(\d+(\.\d+)?)', ep_num_str)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return 0.0
    return 0.0

@app.route('/account')
@login_required
def account_page():
    user_history_query = WatchHistory.query.filter_by(user_id=current_user.id)\
                                        .order_by(WatchHistory.anime_id.asc(), WatchHistory.watched_at.desc())
    all_history_items = user_history_query.all()

    history_by_series_dict = {}
    for item_obj in all_history_items:
        series_key = str(item_obj.anime_id)
        if series_key not in history_by_series_dict:
            history_by_series_dict[series_key] = {
                'anime_id': item_obj.anime_id,
                'title': item_obj.anime_title_romaji or f"Anime ID: {item_obj.anime_id}",
                'cover': item_obj.anime_cover_image or url_for('static', filename='placeholder.png'),
                'episodes': [],
                'most_recent_watched_at_in_series': item_obj.watched_at,
                'max_episode_number_float': 0.0
            }

        current_ep_float = parse_episode_number(item_obj.episode_number_str)
        if current_ep_float > history_by_series_dict[series_key]['max_episode_number_float']:
            history_by_series_dict[series_key]['max_episode_number_float'] = current_ep_float

        if item_obj.watched_at > history_by_series_dict[series_key]['most_recent_watched_at_in_series']:
            history_by_series_dict[series_key]['most_recent_watched_at_in_series'] = item_obj.watched_at

        percentage = 0
        if item_obj.total_duration_seconds and item_obj.total_duration_seconds > 0:
            percentage = round((item_obj.progress_seconds / item_obj.total_duration_seconds) * 100)
            percentage = min(percentage, 100)

        MAX_EPISODES_PER_SERIES_DISPLAY = 3
        if len(history_by_series_dict[series_key]['episodes']) < MAX_EPISODES_PER_SERIES_DISPLAY:
            history_by_series_dict[series_key]['episodes'].append({
                'episode_id': item_obj.consumet_episode_id,
                'episode_number': item_obj.episode_number_str or "Unknown Ep",
                'watched_at_str': item_obj.watched_at.strftime("%b %d, %Y - %H:%M"),
                'percentage': percentage,
                'is_complete': item_obj.is_complete,
                'progress_seconds': item_obj.progress_seconds,
                'total_duration_seconds': item_obj.total_duration_seconds
            })

    AVERAGE_EPISODE_DURATION_SECONDS = 23 * 60
    calculated_total_seconds = 0
    for series_id, data in history_by_series_dict.items():
        num_episodes_assumed_watched = 0
        if data['max_episode_number_float'] > 0:
             num_episodes_assumed_watched = int(round(data['max_episode_number_float'] + 0.01))
        calculated_total_seconds += num_episodes_assumed_watched * AVERAGE_EPISODE_DURATION_SECONDS
        print(f"Series ID {series_id}: Max ep {data['max_episode_number_float']} (parsed as {num_episodes_assumed_watched}), adding {num_episodes_assumed_watched * AVERAGE_EPISODE_DURATION_SECONDS}s")

    total_hours_watched_calculated = 0.0
    if calculated_total_seconds > 0:
        total_hours_watched_calculated = calculated_total_seconds / 3600.0
    print(f"Calculated total seconds: {calculated_total_seconds}, hours: {total_hours_watched_calculated}")

    displayable_series_history_list = list(history_by_series_dict.values())
    displayable_series_history_list.sort(key=lambda s: s['most_recent_watched_at_in_series'], reverse=True)
    MAX_SERIES_BLOCKS_TO_SHOW = 20
    final_history_to_display = displayable_series_history_list[:MAX_SERIES_BLOCKS_TO_SHOW]

    return render_template('account.html',
                           title='My Account',
                           grouped_history=final_history_to_display,
                           total_hours_watched=round(total_hours_watched_calculated, 1),
                           now=datetime.datetime.now(datetime.timezone.utc))

@app.route('/api/home/top_all_time')
def get_home_top_all_time():
    query = f"""query ($page: Int, $perPage: Int, $sort: [MediaSort], $type: MediaType, $isAdult: Boolean) {{ Page(page: $page, perPage: $perPage) {{ media(sort: $sort, type: $type, isAdult: $isAdult) {{ {media_fields_for_list} }} }} }}"""
    variables = {'page': 1, 'perPage': 15, 'sort': "SCORE_DESC", 'type': "ANIME", 'isAdult': False}
    data = query_anilist(query, variables)
    if data and data.get('data') and data['data']['Page'] and data['data']['Page']['media']:
        return jsonify(data['data']['Page']['media'])
    return jsonify({"error": "Could not fetch top all time anime"}), 500

@app.route('/api/home/top_airing')
def get_home_top_airing():
    season, year = get_current_season_and_year()
    seasonal_query = f"""query ($page: Int, $perPage: Int, $sort: [MediaSort], $status: MediaStatus, $season: MediaSeason, $seasonYear: Int, $type: MediaType, $isAdult: Boolean) {{ Page(page: $page, perPage: $perPage) {{ media(sort: $sort, type: $type, isAdult: $isAdult, status: $status, season: $season, seasonYear: $seasonYear) {{ {media_fields_for_list} }} }} }}"""
    variables_seasonal = {'page': 1, 'perPage': 10, 'sort': "TRENDING_DESC", 'status': "RELEASING", 'season': season, 'seasonYear': year, 'type': "ANIME", 'isAdult': False}
    data_seasonal = query_anilist(seasonal_query, variables_seasonal)

    if data_seasonal and data_seasonal.get('data') and data_seasonal['data'].get('Page') and \
       data_seasonal['data']['Page'].get('media') and \
       len(data_seasonal['data']['Page']['media']) > 0:
        return jsonify(data_seasonal['data']['Page']['media'])

    print("Fallback for top_airing: Trying general top airing.")
    general_query = f"""query ($page: Int, $perPage: Int, $sort: [MediaSort], $status: MediaStatus, $type: MediaType, $isAdult: Boolean) {{ Page(page: $page, perPage: $perPage) {{ media(sort: $sort, type: $type, isAdult: $isAdult, status: $status) {{ {media_fields_for_list} }} }} }}"""
    variables_general = {'page': 1, 'perPage': 10, 'sort': "TRENDING_DESC", 'status': "RELEASING", 'type': "ANIME", 'isAdult': False}
    data_general = query_anilist(general_query, variables_general)

    if data_general and data_general.get('data') and data_general['data']['Page'] and data_general['data']['Page']['media']:
        return jsonify(data_general['data']['Page']['media'])

    return jsonify({"error": "Could not fetch top airing anime"}), 500

@app.route('/api/home/popular_this_season')
def get_home_popular_this_season():
    season, year = get_current_season_and_year()
    query_fields = media_fields_for_list
    seasonal_query = f"""query ($page: Int, $perPage: Int, $sort: [MediaSort], $season: MediaSeason, $seasonYear: Int, $type: MediaType, $isAdult: Boolean) {{ Page(page: $page, perPage: $perPage) {{ media(sort: $sort, type: $type, isAdult: $isAdult, season: $season, seasonYear: $seasonYear) {{ {query_fields} }} }} }}"""
    variables_seasonal = {'page': 1, 'perPage': 15, 'sort': "POPULARITY_DESC", 'season': season, 'seasonYear': year, 'type': "ANIME", 'isAdult': False}
    data_seasonal = query_anilist(seasonal_query, variables_seasonal)
    if data_seasonal and data_seasonal.get('data') and data_seasonal['data']['Page'] and data_seasonal['data']['Page']['media'] and len(data_seasonal['data']['Page']['media']) > 0 :
        return jsonify(data_seasonal['data']['Page']['media'])
    print("Fallback for popular_this_season: Trying overall popular.")
    general_query = f"""query ($page: Int, $perPage: Int, $sort: [MediaSort], $type: MediaType, $isAdult: Boolean) {{ Page(page: $page, perPage: $perPage) {{ media(sort: $sort, type: $type, isAdult: $isAdult) {{ {query_fields} }} }} }}"""
    variables_general = {'page': 1, 'perPage': 15, 'sort': "POPULARITY_DESC", 'type': "ANIME", 'isAdult': False}
    data_general = query_anilist(general_query, variables_general)
    if data_general and data_general.get('data') and data_general['data']['Page'] and data_general['data']['Page']['media']:
        return jsonify(data_general['data']['Page']['media'])
    return jsonify({"error": "Could not fetch popular this season"}), 500

@app.route('/api/home/highly_rated_last_year')
def get_home_highly_rated_last_year():
    _, current_year = get_current_season_and_year()
    last_year = current_year - 1
    query = f"""query ($page: Int, $perPage: Int, $sort: [MediaSort], $seasonYear: Int, $type: MediaType, $isAdult: Boolean) {{ Page(page: $page, perPage: $perPage) {{ media(sort: $sort, type: $type, isAdult: $isAdult, seasonYear: $seasonYear) {{ {media_fields_for_list} }} }} }}"""
    variables = {'page': 1, 'perPage': 15, 'sort': "SCORE_DESC", 'seasonYear': last_year, 'type': "ANIME", 'isAdult': False}
    data = query_anilist(query, variables)
    if data and data.get('data') and data['data']['Page'] and data['data']['Page']['media']:
        return jsonify(data['data']['Page']['media'])
    return jsonify({"error": "Could not fetch highly rated last year"}), 500

@app.route('/api/genres')
def get_all_genres_api():
    query = """ query { GenreCollection } """
    data = query_anilist(query, {})
    if data and data.get('data') and data['data'].get('GenreCollection'):
        genres = data['data']['GenreCollection']
        excluded_genres = ["Hentai"]; filtered_genres = [g for g in genres if g and g.lower() not in (eg.lower() for eg in excluded_genres)]
        return jsonify(sorted(filtered_genres))
    return jsonify({"error": "Could not fetch genres"}), 500

@app.route('/api/genre_top_banner_options/<path:genre_name>')
def get_genre_top_banner_options_api(genre_name):
    per_page_for_banner_options = 3
    query = f""" query ($page: Int, $perPage: Int, $sort: [MediaSort], $genre: String, $type: MediaType, $isAdult: Boolean) {{ Page(page: $page, perPage: $perPage) {{ media(sort: $sort, genre: $genre, type: $type, isAdult: $isAdult) {{ id title {{ romaji english }} bannerImage coverImage {{ extraLarge large }} }} }} }}"""
    variables = { 'page': 1, 'perPage': per_page_for_banner_options, 'sort': "POPULARITY_DESC", 'genre': genre_name, 'type': "ANIME", 'isAdult': False }
    data = query_anilist(query, variables)
    if data and data.get('data') and data['data']['Page'] and data['data']['Page'].get('media') and len(data['data']['Page']['media']) > 0:
        return jsonify(data['data']['Page']['media'])
    return jsonify([]), 200

@app.route('/category/<category_key>')
def category_page(category_key):
    page = request.args.get('page', 1, type=int)
    per_page = 20
    category_name_display = "Unknown Category"
    query_to_execute = ""
    variables = {'page': page, 'perPage': per_page, 'type': "ANIME", 'isAdult': False}
    genre_name_param_for_template = request.args.get('name')
    search_query_param_for_template = request.args.get('q')

    query_all_seasonal_filters = f"""query($page: Int, $perPage: Int, $type: MediaType, $isAdult: Boolean, $sort: [MediaSort], $status: MediaStatus, $season: MediaSeason, $seasonYear: Int) {{ Page(page: $page, perPage: $perPage) {{ pageInfo {{ total currentPage lastPage hasNextPage perPage }} media(type: $type, isAdult: $isAdult, sort: $sort, status: $status, season: $season, seasonYear: $seasonYear) {{ {media_fields_for_list} }} }} }}"""
    query_seasonal_no_status = f"""query($page: Int, $perPage: Int, $type: MediaType, $isAdult: Boolean, $sort: [MediaSort], $season: MediaSeason, $seasonYear: Int) {{ Page(page: $page, perPage: $perPage) {{ pageInfo {{ total currentPage lastPage hasNextPage perPage }} media(type: $type, isAdult: $isAdult, sort: $sort, season: $season, seasonYear: $seasonYear) {{ {media_fields_for_list} }} }} }}"""
    query_year_only = f"""query($page: Int, $perPage: Int, $type: MediaType, $isAdult: Boolean, $sort: [MediaSort], $seasonYear: Int) {{ Page(page: $page, perPage: $perPage) {{ pageInfo {{ total currentPage lastPage hasNextPage perPage }} media(type: $type, isAdult: $isAdult, sort: $sort, seasonYear: $seasonYear) {{ {media_fields_for_list} }} }} }}"""
    query_sort_only = f"""query($page: Int, $perPage: Int, $type: MediaType, $isAdult: Boolean, $sort: [MediaSort]) {{ Page(page: $page, perPage: $perPage) {{ pageInfo {{ total currentPage lastPage hasNextPage perPage }} media(type: $type, isAdult: $isAdult, sort: $sort) {{ {media_fields_for_list} }} }} }}"""
    query_search_gql = f"""query($page: Int, $perPage: Int, $type: MediaType, $isAdult: Boolean, $search: String, $sort: [MediaSort]) {{ Page(page: $page, perPage: $perPage) {{ pageInfo {{ total currentPage lastPage hasNextPage perPage }} media(type: $type, isAdult: $isAdult, search: $search, sort: $sort) {{ {media_fields_for_list} }} }} }}"""
    query_status_sort = f"""query($page: Int, $perPage: Int, $type: MediaType, $isAdult: Boolean, $sort: [MediaSort], $status: MediaStatus) {{ Page(page: $page, perPage: $perPage) {{ pageInfo {{ total currentPage lastPage hasNextPage perPage }} media(type: $type, isAdult: $isAdult, sort: $sort, status: $status) {{ {media_fields_for_list} }} }} }}"""
    query_genre_sort = f"""query($page: Int, $perPage: Int, $type: MediaType, $isAdult: Boolean, $sort: [MediaSort], $genre: String) {{ Page(page: $page, perPage: $perPage) {{ pageInfo {{ total currentPage lastPage hasNextPage perPage }} media(type: $type, isAdult: $isAdult, sort: $sort, genre: $genre) {{ {media_fields_for_list} }} }} }}"""
    query_next_episode_releases_paginated = f"""
    query ($page: Int, $perPage: Int, $sort: [MediaSort], $status: MediaStatus, $type: MediaType, $isAdult: Boolean) {{
      Page(page: $page, perPage: $perPage) {{
        pageInfo {{ total currentPage lastPage hasNextPage perPage }}
        media(sort: $sort, status: $status, type: $type, isAdult: $isAdult) {{
          id
          title {{ romaji english userPreferred }}
          coverImage {{ large medium }}
          bannerImage 
          episodes
          airingSchedule(notYetAired: true, perPage: 1) {{
            nodes {{
              airingAt
              timeUntilAiring
              episode
            }}
          }}
        }}
      }}
    }}"""

    if category_key == "top_airing":
        category_name_display = "Top Airing"; season, year = get_current_season_and_year()
        variables.update({'sort': ["TRENDING_DESC", "POPULARITY_DESC"], 'status': "RELEASING", 'season': season, 'seasonYear': year})
        query_to_execute = query_all_seasonal_filters
    elif category_key == "popular_this_season":
        category_name_display = "Popular This Season"; season, year = get_current_season_and_year()
        variables.update({'sort': ["POPULARITY_DESC", "SCORE_DESC"], 'season': season, 'seasonYear': year})
        query_to_execute = query_seasonal_no_status
    elif category_key == "top_all_time":
        category_name_display = "Top Rated All Time"; variables.update({'sort': "SCORE_DESC"})
        query_to_execute = query_sort_only
    elif category_key == "highly_rated_last_year":
        category_name_display = "Highly Rated Last Year"; _, current_year = get_current_season_and_year(); variables.update({'sort': "SCORE_DESC", 'seasonYear': current_year - 1})
        query_to_execute = query_year_only
    elif category_key == "search_results": 
        if not search_query_param_for_template: return redirect(url_for('index'))
        category_name_display = f"Search Results for \"{search_query_param_for_template}\""; variables.update({'search': search_query_param_for_template, 'sort': "SEARCH_MATCH"})
        query_to_execute = query_search_gql
    elif category_key == "genre": 
        if not genre_name_param_for_template: return "Genre name parameter ('name') is required.", 400
        category_name_display = f"{genre_name_param_for_template} Anime"; variables.update({'sort': "POPULARITY_DESC", 'genre': genre_name_param_for_template}); query_to_execute = query_genre_sort
    elif category_key == "next_episode_releases":
        category_name_display = "Next Episode Releases"
        variables.update({
            'sort': ["POPULARITY_DESC"], 
            'status': "RELEASING"
        })
        query_to_execute = query_next_episode_releases_paginated
    else: 
        return "Category not found", 404

    api_response = query_anilist(query_to_execute, variables)
    
    processed_media_list = []
    page_info_from_api = {'currentPage': 1, 'hasNextPage': False, 'total':0, 'lastPage':1}

    if api_response and api_response.get('data') and api_response['data'].get('Page'):
        page_data = api_response['data']['Page']
        page_info_from_api = page_data.get('pageInfo', page_info_from_api)
        media_list_from_api = page_data.get('media', [])

        if category_key == "next_episode_releases":
            current_unix_time = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            valid_upcoming_anime = []
            for anime in media_list_from_api:
                airing_schedule = anime.get('airingSchedule')
                if airing_schedule and airing_schedule.get('nodes') and len(airing_schedule['nodes']) > 0:
                    next_airing = airing_schedule['nodes'][0]
                    airing_at = next_airing.get('airingAt')
                    time_until_airing = next_airing.get('timeUntilAiring')
                    next_episode_number = next_airing.get('episode')

                    if airing_at is not None and time_until_airing is not None and next_episode_number is not None:
                        if airing_at > current_unix_time: 
                            anime_data = {
                                "id": anime.get("id"), "title": anime.get("title"),
                                "coverImage": anime.get("coverImage"), "bannerImage": anime.get("bannerImage"),
                                "episodes": anime.get("episodes"),
                                "nextAiringAt": airing_at, "timeUntilAiring": time_until_airing,
                                "nextEpisodeNumber": next_episode_number
                            }
                            result.append(anime_data)
            
            valid_upcoming_anime.sort(key=lambda x: x.get('timeUntilAiring', float('inf')))
            processed_media_list = valid_upcoming_anime
            page_info_from_api['total'] = len(processed_media_list)
            page_info_from_api['lastPage'] = (len(processed_media_list) + per_page -1) // per_page if per_page > 0 else 1
            page_info_from_api['hasNextPage'] = page < page_info_from_api['lastPage']
        else: 
            processed_media_list = media_list_from_api
        
        if category_key in ["top_airing", "popular_this_season"] and not processed_media_list:
            print(f"No results for '{category_key}' with seasonal filters (page {page}), attempting broader fallback...")
            fallback_variables = {'page': page, 'perPage': per_page, 'type': "ANIME", 'isAdult': False}
            fallback_query_to_execute = ""
            if category_key == "top_airing":
                fallback_variables.update({'sort': ["TRENDING_DESC", "POPULARITY_DESC"], 'status': "RELEASING"})
                fallback_query_to_execute = query_status_sort
            elif category_key == "popular_this_season":
                fallback_variables.update({'sort': ["POPULARITY_DESC", "SCORE_DESC"]})
                fallback_query_to_execute = query_sort_only
            
            if fallback_query_to_execute:
                fallback_api_response = query_anilist(fallback_query_to_execute, fallback_variables)
                if fallback_api_response and fallback_api_response.get('data') and fallback_api_response['data'].get('Page'):
                    page_data = fallback_api_response['data']['Page']
                    processed_media_list = page_data.get('media', [])
                    page_info_from_api = page_data.get('pageInfo', page_info_from_api)

    return render_template('category_page.html', 
                           category_name=category_name_display, category_key=category_key,
                           anime_list=processed_media_list, 
                           page_info=page_info_from_api,
                           search_query=search_query_param_for_template, 
                           genre_name_param=genre_name_param_for_template,
                           now=datetime.datetime.now(datetime.timezone.utc))
@app.route('/search') 
def search_results_page(): return category_page("search_results")

@app.route('/api/consumet/episodes/<int:anilist_id>')
def get_consumet_episodes(anilist_id):
    anilist_title_romaji = request.args.get('titleRomaji', '') 
    anilist_title_english = request.args.get('titleEnglish', '')
    current_provider = DEFAULT_PROVIDER 
    consumet_info_url = ""; mark_as_zoro_specific = False 
    if current_provider.lower() == "zoro":
        if not anilist_title_romaji: return jsonify({"error": "Anime Romaji title required for Zoro ID search."}), 400
        zoro_anime_id = find_zoro_anime_id_from_anilist_titles(anilist_title_romaji, anilist_title_english)
        if zoro_anime_id:
            consumet_info_url = f"{CONSUMET_API_URL}/anime/zoro/info?id={zoro_anime_id}"; mark_as_zoro_specific = True 
        else: 
            consumet_info_url = f"{CONSUMET_API_URL}/meta/anilist/info/{anilist_id}?provider=zoro&subOrDub=sub"; mark_as_zoro_specific = False
    else: 
        is_dub = request.args.get('dub', 'false').lower() == 'true'
        sub_or_dub_val = "dub" if is_dub else "sub"
        consumet_info_url = f"{CONSUMET_API_URL}/meta/anilist/info/{anilist_id}?provider={current_provider}&subOrDub={sub_or_dub_val}"; mark_as_zoro_specific = False
    try:
        response = requests.get(consumet_info_url, timeout=25); response.raise_for_status(); data = response.json()
        if 'episodes' in data and data['episodes'] and len(data['episodes']) > 0:
            for ep in data['episodes']: ep['sourceIsZoroSpecific'] = mark_as_zoro_specific
            return jsonify(data['episodes'])
        title_id = zoro_anime_id if mark_as_zoro_specific and zoro_anime_id else str(anilist_id)
        error_message = data.get('message', f"No episodes for '{title_id}' on '{current_provider}'.")
        return jsonify({"error": error_message, "details": data}), 404
    except Exception as e: return jsonify({"error": "Failed episode data.", "details": str(e)}), 500

@app.route('/api/consumet/stream-link')
def get_consumet_stream_link():
    episode_id = request.args.get('episode_id')
    is_dub_audio_requested = request.args.get('dubAudio', 'false').lower() == 'true'
    source_is_zoro_specific = request.args.get('sourceIsZoroSpecific', 'false').lower() == 'true'
    if not episode_id: return jsonify({"error": "Episode ID is required"}), 400
    consumet_watch_url = ""
    if source_is_zoro_specific:
        dub_param_for_zoro_watch = "true" if is_dub_audio_requested else "false"
        consumet_watch_url = f"{CONSUMET_API_URL}/anime/zoro/watch?episodeId={url_quote(episode_id)}&dub={dub_param_for_zoro_watch}"
    else:
        consumet_watch_url = f"{CONSUMET_API_URL}/meta/anilist/watch/{url_quote(episode_id)}"
    try:
        response = requests.get(consumet_watch_url, timeout=25); response.raise_for_status()
        data = response.json()
        if data.get('sources') and len(data['sources']) > 0: return jsonify(data)
        error_message = data.get('message', f"No sources for ep ID {episode_id} using {consumet_watch_url}.")
        return jsonify({"error": error_message, "details": data}), 404
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/update_watch_progress', methods=['POST'])
@login_required
def update_watch_progress():
    data = request.json
    if not data: return jsonify({"error": "No data provided"}), 400
    anime_id_str = data.get('anime_id'); consumet_ep_id = data.get('episode_id'); ep_num_str = data.get('episode_number_str') 
    progress = data.get('progress_seconds', 0.0); duration = data.get('total_duration_seconds', 0.0)
    anime_title = data.get('anime_title_romaji'); anime_cover = data.get('anime_cover_image')
    if not anime_id_str or not consumet_ep_id: return jsonify({"error": "Missing anime_id or episode_id"}), 400
    try: progress = float(progress); duration = float(duration) if duration else 0.0
    except ValueError: return jsonify({"error": "Invalid progress or duration"}), 400
    history_entry = WatchHistory.query.filter_by(user_id=current_user.id, consumet_episode_id=consumet_ep_id).first()
    is_complete_from_progress = False
    if duration > 0 and progress >= duration * 0.90: is_complete_from_progress = True
    if history_entry:
        if progress > history_entry.progress_seconds + 5 or (is_complete_from_progress and not history_entry.is_complete) :
            history_entry.progress_seconds = progress
            if duration > 0 : history_entry.total_duration_seconds = duration
            history_entry.watched_at = datetime.datetime.now(datetime.timezone.utc)
            history_entry.is_complete = history_entry.is_complete or is_complete_from_progress
            if anime_title and not history_entry.anime_title_romaji: history_entry.anime_title_romaji = anime_title
            if anime_cover and not history_entry.anime_cover_image: history_entry.anime_cover_image = anime_cover
            if ep_num_str and not history_entry.episode_number_str: history_entry.episode_number_str = ep_num_str
        else: return jsonify({"message": "Progress not significantly changed."}), 200
    else:
        history_entry = WatchHistory(user_id=current_user.id, anime_id=str(anime_id_str), anime_title_romaji=anime_title, anime_cover_image=anime_cover, consumet_episode_id=consumet_ep_id, episode_number_str=ep_num_str, progress_seconds=progress, total_duration_seconds=duration, is_complete=is_complete_from_progress, watched_at=datetime.datetime.now(datetime.timezone.utc))
        db.session.add(history_entry)
    try: db.session.commit(); return jsonify({"message": "Watch progress updated."}), 200
    except Exception as e: db.session.rollback(); print(f"DB Error: {e}"); return jsonify({"error": "DB error."}), 500

def rewrite_m3u8_content_recursive(m3u8_content_str, original_m3u8_full_url, referer_for_proxied_files):
    parsed_original_url = urlparse(original_m3u8_full_url); path_segments = parsed_original_url.path.split('/'); base_path_for_this_m3u8 = "/"
    if len(path_segments) > 1:
        if parsed_original_url.path.endswith('/'): base_path_for_this_m3u8 = parsed_original_url.path
        else: base_path_for_this_m3u8 = "/".join(path_segments[:-1]) + "/"
    base_url_for_relative_paths_in_this_m3u8 = f"{parsed_original_url.scheme}://{parsed_original_url.netloc}{base_path_for_this_m3u8}"
    rewritten_lines = []; quoted_referer = url_quote(referer_for_proxied_files if referer_for_proxied_files else '')
    for line in m3u8_content_str.splitlines():
        line = line.strip(); is_url_line = line and not line.startswith('#')
        if is_url_line:
            absolute_original_media_url = line if (line.startswith('http://') or line.startswith('https://')) else urljoin(base_url_for_relative_paths_in_this_m3u8, line)
            new_line = f"/api/proxy/media_file?url={url_quote(absolute_original_media_url)}&referer={quoted_referer}"; rewritten_lines.append(new_line)
        else: rewritten_lines.append(line)
    return "\n".join(rewritten_lines)

@app.route('/api/proxy/m3u8')
def proxy_master_m3u8():
    target_url = request.args.get('url'); referer = request.args.get('referer')
    if not target_url: return jsonify({"error": "Master M3U8 URL required"}), 400
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
    if referer: headers['Referer'] = referer
    try:
        s = requests.Session(); proxied_response = s.get(target_url, headers=headers, timeout=20)
        proxied_response.raise_for_status(); ct = proxied_response.headers.get('Content-Type', 'application/vnd.apple.mpegurl')
        content = proxied_response.text; final_m3u8 = rewrite_m3u8_content_recursive(content, target_url, referer)
        return Response(final_m3u8, content_type=ct, status=200)
    except Exception as e: print(f"Failed proxy MASTER M3U8 {target_url}: {e}"); return jsonify({"error": str(e)}), 502

@app.route('/api/proxy/media_file')
def proxy_media_file():
    target_url = request.args.get('url'); referer = request.args.get('referer')
    if not target_url: return jsonify({"error": "Media file URL required"}), 400
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
    if referer: headers['Referer'] = referer
    s = requests.Session()
    try:
        is_sub_m3u8 = '.m3u8' in target_url.lower()
        if is_sub_m3u8:
            resp = s.get(target_url, headers=headers, timeout=20); resp.raise_for_status()
            content = resp.text; ct = resp.headers.get('Content-Type', 'application/vnd.apple.mpegurl')
            rewritten_content = rewrite_m3u8_content_recursive(content, target_url, referer)
            return Response(rewritten_content, content_type=ct, status=200)
        else:
            resp = s.get(target_url, headers=headers, stream=True, timeout=45); resp.raise_for_status()
            ct = resp.headers.get('Content-Type', 'application/octet-stream')
            if '.ts' in target_url.lower(): ct = 'video/MP2T'
            elif '.aac' in target_url.lower(): ct = 'audio/aac'
            def gen():
                for chunk in resp.iter_content(524288): yield chunk
            return Response(gen(), content_type=ct, status=resp.status_code)
    except Exception as e: return jsonify({"error": str(e)}), 502

@app.route('/api/proxy/subtitle')
def proxy_subtitle():
    target_url = request.args.get('url'); referer = request.args.get('referer') 
    if not target_url: return jsonify({"error": "Subtitle URL required"}), 400
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
    if referer: headers['Referer'] = referer
    try:
        s = requests.Session(); resp = s.get(target_url, headers=headers, stream=True, timeout=15)
        resp.raise_for_status(); ct = resp.headers.get('Content-Type', 'text/vtt')
        def gen():
            for chunk in resp.iter_content(4096): yield chunk
        return Response(gen(), content_type=ct, status=resp.status_code)
    except Exception as e: return jsonify({"error": str(e)}), 502

@app.route('/trigger_video_download')
@login_required 
def trigger_video_download():
    consumet_episode_id = request.args.get('episode_id_for_consumet') 
    is_dub_audio_requested_str = request.args.get('dub_audio', 'false')
    source_is_zoro_specific_str = request.args.get('source_is_zoro_specific', 'false')
    
    selected_quality_label = request.args.get('quality_label', 'default') 
    output_format = request.args.get('format', 'mp4').lower()
    anime_title_base = request.args.get('anime_title', 'video')
    ep_num_base = request.args.get('ep_num', '')

    if not consumet_episode_id:
        flash("Episode information missing for download.", "danger")
        return redirect(request.referrer or url_for('index'))
    if output_format not in ['mp4', 'mkv']:
        flash("Invalid output format selected.", "danger")
        return redirect(request.referrer or url_for('index'))

    is_dub_audio_requested = is_dub_audio_requested_str.lower() == 'true'
    source_is_zoro_specific = source_is_zoro_specific_str.lower() == 'true'

    watch_url = ""
    if source_is_zoro_specific:
        dub_param_for_zoro_watch = "true" if is_dub_audio_requested else "false"
        watch_url = f"{CONSUMET_API_URL}/anime/zoro/watch?episodeId={url_quote(consumet_episode_id)}&dub={dub_param_for_zoro_watch}"
    else:
        watch_url = f"{CONSUMET_API_URL}/meta/anilist/watch/{url_quote(consumet_episode_id)}"
    
    print(f"Trigger Download: Re-fetching stream data from: {watch_url}")
    actual_m3u8_to_download = None
    actual_referer = None
    error_message = None

    try:
        res = requests.get(watch_url, timeout=20)
        res.raise_for_status()
        stream_data_json = res.json()

        if stream_data_json and stream_data_json.get('sources') and len(stream_data_json['sources']) > 0:
            target_source = None
            normalized_selected_quality = selected_quality_label.lower()
            for s in stream_data_json['sources']:
                source_quality_lower = str(s.get('quality', '')).lower()
                if source_quality_lower == normalized_selected_quality and s.get('url', '').endswith('.m3u8'):
                    target_source = s; break
            if not target_source:
                for s in stream_data_json['sources']:
                    if s.get('url', '').endswith('.m3u8'):
                        target_source = s
                        if selected_quality_label == 'default' and s.get('quality'): selected_quality_label = str(s.get('quality'))
                        break 
            if target_source:
                actual_m3u8_to_download = target_source['url']
                if stream_data_json.get('headers') and stream_data_json['headers'].get('Referer'):
                    actual_referer = stream_data_json['headers']['Referer']
            else: error_message = "Selected quality or any M3U8 source not found in fresh fetch."
        else: error_message = stream_data_json.get('message', "No video sources in fresh fetch.") if stream_data_json else "Empty response."
    except Exception as e: error_message = f"Could not re-fetch stream data: {str(e)}"

    if error_message or not actual_m3u8_to_download:
        flash(error_message or "Could not get M3U8 URL.", "danger")
        return redirect(request.referrer or url_for('index'))

    safe_anime_title = secure_filename(anime_title_base)
    safe_ep_num = secure_filename(ep_num_base)
    safe_quality_label = secure_filename(selected_quality_label.replace(" ", "_"))
    output_filename_final = f"{safe_anime_title}_Ep{safe_ep_num}_{safe_quality_label}.{output_format}"
    
    temp_dir = None
    try:
        # Create a temporary directory for FFmpeg output. This is local to the server.
        temp_dir = tempfile.mkdtemp()
        temp_output_path = os.path.join(temp_dir, output_filename_final)
        
        print(f"Target M3U8 for FFmpeg: {actual_m3u8_to_download}")
        print(f"Temporary output path for FFmpeg: {temp_output_path}")
        if actual_referer: print(f"Using Referer: {actual_referer}")

        # FFmpeg command. Assumes 'ffmpeg' is in the server's PATH.
        ffmpeg_cmd = ['ffmpeg'] # Reverted: Uses system-installed ffmpeg
        
        # Add headers if needed (Referer is common for some sources)
        if actual_referer:
            ffmpeg_cmd.extend(['-headers', f'Referer: {actual_referer}\r\nUser-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36\r\n'])

        ffmpeg_cmd.extend([
            '-i', actual_m3u8_to_download,  # Input M3U8 URL
            '-c', 'copy',                   # Copy codecs (no re-encoding, faster)
            '-bsf:a', 'aac_adtstoasc',      # Bitstream filter often needed for AAC audio in MP4 container
            temp_output_path                # Output file path
        ])
        
        print(f"Running FFmpeg command: {' '.join(ffmpeg_cmd)}")
        
        # Using Popen for better control and to avoid blocking indefinitely if ffmpeg hangs on prompt
        # creationflags=subprocess.CREATE_NO_WINDOW is for Windows, ignored on Linux servers.
        process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, 
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        
        try:
            stdout, stderr = process.communicate(timeout=1800) # 30 minutes timeout
        except subprocess.TimeoutExpired:
            process.kill() # Kill the process if it times out
            stdout, stderr = process.communicate() # Get any output before it was killed
            print("FFmpeg process timed out after 30 minutes.")
            flash("Download process timed out.", "danger")
            if temp_dir and os.path.exists(temp_dir): shutil.rmtree(temp_dir)
            return redirect(request.referrer or url_for('index'))

        print("-------------- FFmpeg STDOUT --------------")
        print(stdout if stdout else "No STDOUT")
        print("-------------- FFmpeg STDERR --------------")
        print(stderr if stderr else "No STDERR") # FFmpeg often prints progress/info to stderr
        print("-------------------------------------------")

        if process.returncode == 0 and os.path.exists(temp_output_path) and os.path.getsize(temp_output_path) > 1024:
            print(f"FFmpeg download/muxing successful: {temp_output_path}")
            # Use send_file to send the generated file to the user
            return send_file(temp_output_path, as_attachment=True, download_name=output_filename_final)
        else:
            print(f"FFmpeg Error (code {process.returncode}). Output: {temp_output_path}, Exists: {os.path.exists(temp_output_path)}")
            error_message_from_stderr = "FFmpeg process failed. Check server logs for details."
            if stderr:
                lines = stderr.strip().splitlines()
                # Try to find a relevant error line from ffmpeg's output
                for line in reversed(lines):
                    if "error" in line.lower() or "failed" in line.lower() or "cannot open" in line.lower():
                        error_message_from_stderr = line
                        break
                if error_message_from_stderr == "FFmpeg process failed. Check server logs for details." and lines:
                    error_message_from_stderr = lines[-1] # Last line as fallback
            flash(f"Download processing failed. Detail: {error_message_from_stderr[:250]}...", "danger")
            return redirect(request.referrer or url_for('index'))

    except Exception as e: # Catch any other unexpected Python exceptions
        print(f"General exception during download process: {e}")
        flash(f"An unexpected error occurred during download: {str(e)[:100]}", "danger")
        return redirect(request.referrer or url_for('index'))
    finally:
        # Always attempt to clean up the temporary directory
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                print(f"Cleaned up temp directory: {temp_dir}")
            except Exception as e_clean:
                print(f"Error cleaning up temp directory {temp_dir}: {e_clean}")

@app.route('/download_options')
@login_required 
def download_options_page_route(): 
    consumet_episode_id = request.args.get('episodeId')
    anilist_id_str = request.args.get('anilistId')
    anime_title_romaji = request.args.get('titleRomaji', 'Unknown Anime')
    episode_number_str = request.args.get('epNum', 'Episode')
    user_preferred_dub_audio_str = request.args.get('dubAudio', 'false')
    source_is_zoro_specific_str = request.args.get('sourceIsZoroSpecific', 'false')
    
    is_dub_audio_requested = user_preferred_dub_audio_str.lower() == 'true'
    source_is_zoro_specific = source_is_zoro_specific_str.lower() == 'true'

    if not consumet_episode_id or not anilist_id_str:
        flash("Missing necessary information to prepare download options.", "danger")
        if anilist_id_str:
            return redirect(url_for('anime_details_page', anime_id=int(anilist_id_str)))
        return redirect(url_for('index'))

    watch_url = ""
    if source_is_zoro_specific:
        dub_param_for_zoro_watch = "true" if is_dub_audio_requested else "false"
        watch_url = f"{CONSUMET_API_URL}/anime/zoro/watch?episodeId={url_quote(consumet_episode_id)}&dub={dub_param_for_zoro_watch}"
    else:
        watch_url = f"{CONSUMET_API_URL}/meta/anilist/watch/{url_quote(consumet_episode_id)}"
        
    stream_data_for_download = None
    error_message_stream = None
    try:
        res = requests.get(watch_url, timeout=20)
        res.raise_for_status()
        stream_data_json = res.json()
        if not (stream_data_json.get('sources') and len(stream_data_json['sources']) > 0):
            error_message_stream = stream_data_json.get('message', "No video sources found by Consumet for this episode version.")
        else:
            stream_data_for_download = stream_data_json 
    except Exception as e:
        print(f"Error fetching stream data for download options: {e}")
        error_message_stream = f"Could not fetch stream data: {str(e)}"

    return render_template('download_episode.html',
                           anime_title=anime_title_romaji,
                           episode_number=episode_number_str,
                           consumet_episode_id=consumet_episode_id, 
                           anilist_id=anilist_id_str, 
                           stream_data=stream_data_for_download, 
                           error_message=error_message_stream,
                           source_is_zoro_specific=source_is_zoro_specific, 
                           current_dub_preference=is_dub_audio_requested, 
                           now=datetime.datetime.now(datetime.timezone.utc))

@app.route('/api/home/next_episode_releases')
def get_home_next_episode_releases():
    current_unix_time = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    
    query = f"""
    query ($page: Int, $perPage: Int, $sortMedia: [MediaSort], $status: MediaStatus, $type: MediaType, $isAdult: Boolean) {{
      Page(page: $page, perPage: $perPage) {{
        media(sort: $sortMedia, status: $status, type: $type, isAdult: $isAdult) {{
          id
          title {{ romaji english userPreferred }}
          coverImage {{ large medium }} 
          bannerImage 
          episodes
          airingSchedule(notYetAired: true, perPage: 1) {{
            nodes {{
              airingAt
              timeUntilAiring
              episode
            }}
          }}
        }}
      }}
    }}"""
    variables = {
        'page': 1, 
        'perPage': 30, 
        'sortMedia': ["POPULARITY_DESC"], 
        'status': "RELEASING", 
        'type': "ANIME", 
        'isAdult': False
    }
    
    print(f"Fetching for /api/home/next_episode_releases with vars: {variables}")
    anilist_response_data = query_anilist(query, variables)
    
    if anilist_response_data is None: 
        print(f"Anilist query failed for /api/home/next_episode_releases (query_anilist returned None).")
        return jsonify({"error": "Failed to fetch data from Anilist for next episode releases."}), 500

    results = []
    if not (anilist_response_data.get('data') and \
            anilist_response_data['data'].get('Page') and \
            'media' in anilist_response_data['data']['Page']):
        print(f"Unexpected data structure from Anilist for /api/home/next_episode_releases. Response: {anilist_response_data}")
        return jsonify({"error": "Could not fetch next episode releases (unexpected data structure from Anilist)"}), 500

    media_items = anilist_response_data['data']['Page']['media']
    print(f"Received {len(media_items)} media items for next_episode_releases processing.")
        
    for anime in media_items: 
        airing_schedule = anime.get('airingSchedule')
        if airing_schedule and airing_schedule.get('nodes') and len(airing_schedule['nodes']) > 0:
            next_airing = airing_schedule['nodes'][0]
            
            airing_at = next_airing.get('airingAt')
            time_until_airing = next_airing.get('timeUntilAiring')
            next_episode_number = next_airing.get('episode')

            if airing_at is not None and time_until_airing is not None and next_episode_number is not None:
                if airing_at > current_unix_time: 
                    anime_data = {
                        "id": anime.get("id"), "title": anime.get("title"),
                        "coverImage": anime.get("coverImage"), "bannerImage": anime.get("bannerImage"),
                        "episodes": anime.get("episodes"),
                        "nextAiringAt": airing_at, "timeUntilAiring": time_until_airing,
                        "nextEpisodeNumber": next_episode_number
                    }
                    results.append(anime_data)
    
    results.sort(key=lambda x: x.get('timeUntilAiring', float('inf')))
    print(f"Processed {len(results)} anime with upcoming episodes for next_episode_releases.")
    return jsonify(results[:15])


@app.route('/api/search_suggestions')
def get_search_suggestions():
    query_param = request.args.get('q', '').strip()
    if not query_param:
        return jsonify([])

    graphql_query = """
    query ($search: String, $perPage: Int) {
      Page(perPage: $perPage) {
        media(search: $search, type: ANIME, isAdult: false, sort: SEARCH_MATCH) {
          id title { romaji english userPreferred } coverImage { medium }
        }
      }
    }"""
    variables = {'search': query_param, 'perPage': 7} 
    data = query_anilist(graphql_query, variables)
    if data and data.get('data') and data['data'].get('Page') and data['data']['Page'].get('media'):
        suggestions = []
        for item in data['data']['Page']['media']:
            title = item.get('title', {})
            display_title = title.get('userPreferred') or title.get('romaji') or title.get('english')
            if display_title:
                suggestions.append({
                    'id': item['id'],
                    'title': display_title,
                    'coverImage': item.get('coverImage', {}).get('medium')
                })
        return jsonify(suggestions)
    return jsonify([])

# --- Database Initialization (Revised for Cloud) ---
def init_db_on_startup(app_instance):
    # When running in a deployed environment, the DB file might not exist initially.
    # db.engine.connect() is used to check for tables more robustly.
    with app_instance.app_context():
        try:
            # Attempt to connect and check for a known table.
            # 'user' table is a good candidate for existence check.
            # Note: db.engine.connect() needs to be handled carefully, it opens a connection.
            # For a simple check, just accessing db.metadata might be enough for a new DB.
            # However, `has_table` is more definitive.
            # For SQLite, it's generally safe.
            inspector = db.inspect(db.engine)
            if not inspector.has_table("user"):
                print("Database tables not found. Creating all tables now...")
                db.create_all()
                print("Database tables created successfully.")
            else:
                print("Database tables already exist. Skipping creation.")
        except Exception as e:
            # This could happen if the database file is totally missing or corrupted,
            # but create_all() would usually handle missing file.
            print(f"Error during database initialization check: {e}")
            # As a fallback, try creating all tables anyway, which usually just
            # creates missing tables and ignores existing ones.
            print("Attempting db.create_all() as a fallback for initialization.")
            db.create_all()


# --- Main entry point for local development/testing ---
if __name__ == '__main__':
    # Initialize DB (creates if needed)
    init_db_on_startup(app)
    
    # For local development, still bind to localhost:5000 by default.
    # In deployment (e.g., Render), the platform handles host/port.
    print(f"Flask app starting in local development mode on http://127.0.0.1:5000")
    app.run(debug=True, host='127.0.0.1', port=5000)
