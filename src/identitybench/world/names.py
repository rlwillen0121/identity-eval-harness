"""Names, locales and job titles for synthetic identities (SPEC §6.2, §6.4, §8.5).

Names are drawn from twelve locale groups so that name-swap invariance twins (SPEC §8.5) can
move an identity across locales and double as a basic fairness check. Each country maps to a
weighted mix of locales: mostly local names, plus the diaspora mix a real office has (a London
office has Indian and Nigerian surnames too). That keeps names plausible for the identity's
country without making locale a proxy for location.

Titles have a canonical form (used for peer groups) and a noisy surface form shown to models.
No rule reads titles, so noise here only tests that models don't key on surface forms.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

LOCALES: tuple[str, ...] = ("US", "UK", "DE", "FR", "ES", "IN", "JP", "CN", "BR", "MX", "NG", "KR")

FIRST_NAMES: dict[str, tuple[str, ...]] = {
    "US": (
        "James", "Mary", "Michael", "Patricia", "Robert", "Jennifer", "John", "Linda", "David",
        "Elizabeth", "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica", "Thomas",
        "Sarah", "Christopher", "Karen", "Charles", "Lisa", "Daniel", "Nancy", "Matthew",
        "Betty", "Anthony", "Sandra", "Mark", "Ashley", "Donald", "Kimberly", "Steven", "Emily",
        "Andrew", "Donna", "Paul", "Michelle", "Joshua", "Carol", "Kenneth", "Amanda", "Kevin",
        "Melissa", "Brian", "Deborah", "Timothy", "Stephanie", "Ryan", "Rebecca", "Jason",
        "Laura", "Tyler", "Hannah", "Brandon", "Megan", "Jordan", "Taylor", "Dylan", "Kayla",
        "Marcus", "Jasmine", "Darnell", "Aaliyah", "Tyrone", "Imani",
    ),
    "UK": (
        "Oliver", "Amelia", "George", "Olivia", "Harry", "Isla", "Jack", "Ava", "Jacob", "Emily",
        "Charlie", "Sophie", "Thomas", "Grace", "Oscar", "Mia", "William", "Poppy", "James",
        "Ella", "Alfie", "Lily", "Joshua", "Evie", "Freddie", "Isabella", "Archie", "Charlotte",
        "Henry", "Jessica", "Leo", "Daisy", "Alexander", "Phoebe", "Edward", "Florence",
        "Samuel", "Freya", "Joseph", "Alice", "Max", "Ruby", "Daniel", "Imogen", "Lewis",
        "Holly", "Callum", "Eleanor", "Rhys", "Niamh", "Owen", "Siobhan", "Ewan", "Fiona",
        "Gareth", "Bethan", "Hamish", "Catriona",
    ),
    "DE": (
        "Lukas", "Anna", "Leon", "Mia", "Finn", "Emma", "Jonas", "Hannah", "Paul", "Lena",
        "Felix", "Lea", "Maximilian", "Laura", "Elias", "Sophie", "Noah", "Marie", "Ben", "Lina",
        "Luis", "Johanna", "Tim", "Katharina", "Jan", "Julia", "Moritz", "Sarah", "Niklas",
        "Clara", "Tobias", "Charlotte", "Sebastian", "Franziska", "Florian", "Miriam", "Stefan",
        "Sabine", "Andreas", "Petra", "Thomas", "Claudia", "Michael", "Andrea", "Jürgen", "Ute",
        "Matthias", "Anja", "Markus", "Birgit", "Dominik", "Svenja", "Philipp", "Annika", "Jens",
        "Heike",
    ),
    "FR": (
        "Lucas", "Emma", "Hugo", "Léa", "Louis", "Chloé", "Gabriel", "Manon", "Arthur",
        "Camille", "Jules", "Inès", "Raphaël", "Sarah", "Adam", "Jade", "Nathan", "Louise",
        "Théo", "Alice", "Mathis", "Lola", "Tom", "Juliette", "Clément", "Margaux", "Antoine",
        "Pauline", "Maxime", "Mathilde", "Nicolas", "Élodie", "Julien", "Céline", "Pierre",
        "Sophie", "Thomas", "Aurélie", "Alexandre", "Claire", "Romain", "Émilie", "Guillaume",
        "Isabelle", "François", "Nathalie", "Olivier", "Sandrine", "Sébastien", "Valérie",
        "Benoît", "Hélène", "Yannick", "Amandine", "Mehdi", "Yasmine",
    ),
    "ES": (
        "Hugo", "Lucía", "Martín", "Sofía", "Pablo", "María", "Alejandro", "Martina", "Lucas",
        "Paula", "Daniel", "Julia", "Mateo", "Valeria", "Adrián", "Daniela", "Álvaro", "Carmen",
        "Diego", "Elena", "Javier", "Laura", "Sergio", "Ana", "Carlos", "Marta", "Manuel",
        "Cristina", "Antonio", "Isabel", "José", "Pilar", "Francisco", "Rosa", "David", "Raquel",
        "Jorge", "Beatriz", "Fernando", "Nuria", "Rubén", "Silvia", "Iván", "Patricia", "Raúl",
        "Alba", "Íñigo", "Ainhoa", "Jaime", "Irene", "Gonzalo", "Marina", "Enrique", "Rocío",
    ),
    "IN": (
        "Aarav", "Priya", "Vivaan", "Ananya", "Aditya", "Diya", "Arjun", "Isha", "Rohan",
        "Kavya", "Rahul", "Sneha", "Vikram", "Pooja", "Siddharth", "Neha", "Karan", "Divya",
        "Amit", "Anjali", "Rajesh", "Deepika", "Suresh", "Lakshmi", "Arun", "Meera", "Sanjay",
        "Swati", "Nikhil", "Shreya", "Varun", "Aishwarya", "Manish", "Ritu", "Abhishek",
        "Nandini", "Pranav", "Sunita", "Harish", "Revathi", "Gaurav", "Shalini", "Ravi",
        "Preeti", "Kiran", "Aparna", "Venkat", "Bhavna", "Imran", "Farah", "Harpreet",
        "Gurpreet", "Sandeep", "Tanvi", "Ashwin", "Keerthi",
    ),
    "JP": (
        "Haruto", "Yui", "Sota", "Hina", "Yuto", "Aoi", "Riku", "Sakura", "Ren", "Yuna",
        "Hayato", "Mei", "Kaito", "Rin", "Takumi", "Akari", "Daiki", "Koharu", "Kenta", "Misaki",
        "Shota", "Nanami", "Yusuke", "Ayaka", "Takeshi", "Yuki", "Hiroshi", "Keiko", "Kenji",
        "Naoko", "Satoshi", "Tomoko", "Kazuki", "Emi", "Ryota", "Mai", "Tatsuya", "Yoko",
        "Naoki", "Kana", "Shun", "Chihiro", "Daisuke", "Asuka", "Masato", "Erika", "Kohei",
        "Saki", "Taro", "Hanako", "Makoto", "Ayumi",
    ),
    "CN": (
        "Wei", "Fang", "Jie", "Li", "Hao", "Xin", "Yang", "Jing", "Lei", "Ying", "Jun", "Min",
        "Tao", "Yan", "Bo", "Hui", "Peng", "Lan", "Qiang", "Mei", "Gang", "Na", "Yong", "Xia",
        "Ming", "Hong", "Feng", "Juan", "Kai", "Ting", "Bin", "Yu", "Long", "Lin", "Rui", "Qing",
        "Zhen", "Xiaoyu", "Haoran", "Yiming", "Zihan", "Yuxuan", "Jiahui", "Xinyi", "Zhiwei",
        "Shuang", "Wenjie", "Yifan", "Ruoxi", "Junjie", "Meiling", "Chenxi", "Siyu", "Zhihao",
        "Lingling", "Jianguo",
    ),
    "BR": (
        "Miguel", "Alice", "Arthur", "Sophia", "Heitor", "Helena", "Bernardo", "Valentina",
        "Davi", "Laura", "Théo", "Isabella", "Lorenzo", "Manuela", "Gabriel", "Júlia", "Pedro",
        "Heloísa", "Benjamin", "Luiza", "Matheus", "Maria Eduarda", "Lucas", "Lorena", "Nicolas",
        "Lívia", "Guilherme", "Giovanna", "Rafael", "Beatriz", "Gustavo", "Mariana", "Felipe",
        "Larissa", "João", "Camila", "Thiago", "Fernanda", "Bruno", "Juliana", "Rodrigo",
        "Patrícia", "Leonardo", "Aline", "Vinícius", "Renata", "Eduardo", "Tatiana", "Caio",
        "Bianca", "Diego", "Letícia",
    ),
    "MX": (
        "Santiago", "Sofía", "Mateo", "Valentina", "Sebastián", "Regina", "Leonardo", "Camila",
        "Emiliano", "Ximena", "Diego", "María José", "Daniel", "Fernanda", "Miguel Ángel",
        "Renata", "Alejandro", "Andrea", "José Luis", "Guadalupe", "Juan Carlos", "Mariana",
        "Luis", "Daniela", "Jorge", "Gabriela", "Ricardo", "Alejandra", "Eduardo", "Paola",
        "Fernando", "Verónica", "Francisco", "Adriana", "Héctor", "Claudia", "Arturo", "Mónica",
        "Raúl", "Rosa", "Javier", "Leticia", "Roberto", "Elena", "Óscar", "Itzel", "Rodrigo",
        "Citlali", "Manuel", "Yesenia", "Iker", "Natalia",
    ),
    "NG": (
        "Chinedu", "Ngozi", "Oluwaseun", "Funmilayo", "Emeka", "Adaeze", "Tunde", "Folake",
        "Ifeanyi", "Chiamaka", "Babatunde", "Yetunde", "Obinna", "Nneka", "Segun", "Bukola",
        "Chukwuemeka", "Amaka", "Olumide", "Temitope", "Kelechi", "Uchenna", "Adebayo",
        "Omolara", "Ikenna", "Chioma", "Femi", "Kemi", "Nnamdi", "Ebele", "Damilola", "Titilayo",
        "Kayode", "Ifeoma", "Musa", "Aisha", "Ibrahim", "Zainab", "Abubakar", "Hauwa", "Yusuf",
        "Halima", "Chidi", "Onyinye", "Tobi", "Simisola", "Gbenga", "Blessing", "Ayodele",
        "Precious", "Uche", "Ijeoma",
    ),
    "KR": (
        "Min-jun", "Seo-yeon", "Ji-ho", "Ha-eun", "Do-yun", "Ji-woo", "Seo-jun", "Seo-yun",
        "Ha-jun", "Min-seo", "Joon-ho", "Ji-min", "Hyun-woo", "Soo-ah", "Ye-jun", "Ye-eun",
        "Jae-won", "Chae-won", "Dong-hyun", "Su-bin", "Sung-min", "Yu-na", "Tae-yang", "Da-eun",
        "Jin-woo", "Eun-ji", "Sang-hoon", "Hye-jin", "Young-ho", "Mi-kyung", "Kyung-soo",
        "Soo-jin", "Jung-hoon", "Na-yeon", "Seung-woo", "Ji-yeon", "Hyun-jun", "So-yeon",
        "Woo-jin", "Yeon-woo", "Jun-young", "Bo-ra", "Min-ho", "Hae-won", "Jae-hyun", "Seul-gi",
        "Tae-hyun", "Ga-young", "Kwang-soo", "In-hye", "Byung-chul", "Mi-rae",
    ),
}  # fmt: skip

LAST_NAMES: dict[str, tuple[str, ...]] = {
    "US": (
        "Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis", "Wilson",
        "Anderson", "Taylor", "Thomas", "Moore", "Jackson", "Martin", "Thompson", "White",
        "Harris", "Clark", "Lewis", "Robinson", "Walker", "Young", "Allen", "King", "Wright",
        "Scott", "Hill", "Green", "Adams", "Baker", "Nelson", "Carter", "Mitchell", "Roberts",
        "Turner", "Phillips", "Campbell", "Parker", "Evans", "Edwards", "Collins", "Stewart",
        "Morris", "Murphy", "Cook", "Rogers", "Morgan", "Cooper", "Peterson", "Reed", "Bailey",
        "Bell", "Kelly", "Howard", "Ward", "Cox", "Brooks", "Bennett", "Gray", "Hughes", "Price",
        "Sanders", "Myers", "Long", "Foster", "Russell", "Coleman", "Washington", "Jenkins",
        "Butler",
    ),
    "UK": (
        "Smith", "Jones", "Williams", "Taylor", "Brown", "Davies", "Evans", "Wilson", "Thomas",
        "Johnson", "Roberts", "Robinson", "Thompson", "Wright", "Walker", "White", "Edwards",
        "Hughes", "Green", "Hall", "Lewis", "Harris", "Clarke", "Jackson", "Wood", "Turner",
        "Martin", "Cooper", "Hill", "Ward", "Morris", "Moore", "Clark", "Lee", "King", "Baker",
        "Harrison", "Morgan", "Allen", "James", "Scott", "Phillips", "Watson", "Davis", "Parker",
        "Price", "Bennett", "Young", "Griffiths", "Mitchell", "Kelly", "Cook", "Carter",
        "Richardson", "Bailey", "Collins", "Bell", "Shaw", "Murphy", "Miller", "Cox", "Richards",
        "Fletcher", "Pritchard", "Llewellyn", "MacDonald", "Campbell", "Stewart", "Fraser",
    ),
    "DE": (
        "Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker",
        "Schulz", "Hoffmann", "Schäfer", "Koch", "Bauer", "Richter", "Klein", "Wolf", "Schröder",
        "Neumann", "Schwarz", "Zimmermann", "Braun", "Krüger", "Hofmann", "Hartmann", "Lange",
        "Schmitt", "Werner", "Schmitz", "Krause", "Meier", "Lehmann", "Schmid", "Schulze",
        "Maier", "Köhler", "Herrmann", "König", "Walter", "Mayer", "Huber", "Kaiser", "Fuchs",
        "Peters", "Lang", "Scholz", "Möller", "Weiß", "Jung", "Hahn", "Schubert", "Vogel",
        "Friedrich", "Keller", "Günther", "Frank", "Berger",
    ),
    "FR": (
        "Martin", "Bernard", "Dubois", "Thomas", "Robert", "Richard", "Petit", "Durand", "Leroy",
        "Moreau", "Simon", "Laurent", "Lefebvre", "Michel", "David", "Bertrand", "Roux",
        "Vincent", "Fournier", "Morel", "Girard", "André", "Lefèvre", "Mercier", "Dupont",
        "Lambert", "Bonnet", "François", "Legrand", "Garnier", "Faure", "Rousseau", "Blanc",
        "Guérin", "Muller", "Henry", "Roussel", "Nicolas", "Perrin", "Morin", "Mathieu",
        "Clément", "Gauthier", "Dumont", "Fontaine", "Chevalier", "Robin", "Masson", "Gérard",
        "Nguyen", "Boyer", "Denis", "Lemaire", "Duval", "Joly", "Gautier", "Roger", "Roche",
        "Benoît",
    ),
    "ES": (
        "García", "Rodríguez", "González", "Fernández", "López", "Martínez", "Sánchez", "Pérez",
        "Gómez", "Martín", "Jiménez", "Ruiz", "Hernández", "Díaz", "Moreno", "Muñoz", "Álvarez",
        "Romero", "Alonso", "Gutiérrez", "Navarro", "Torres", "Domínguez", "Vázquez", "Ramos",
        "Gil", "Ramírez", "Serrano", "Blanco", "Molina", "Morales", "Suárez", "Ortega",
        "Delgado", "Castro", "Ortiz", "Rubio", "Marín", "Sanz", "Núñez", "Iglesias", "Medina",
        "Garrido", "Cortés", "Castillo", "Santos", "Lozano", "Guerrero", "Cano", "Prieto",
        "Méndez", "Cruz", "Calvo", "Gallego", "Vidal", "León", "Herrera", "Márquez", "Peña",
        "Cabrera",
    ),
    "IN": (
        "Sharma", "Verma", "Gupta", "Patel", "Singh", "Kumar", "Reddy", "Rao", "Iyer", "Nair",
        "Menon", "Pillai", "Joshi", "Mehta", "Shah", "Desai", "Kulkarni", "Chopra", "Malhotra",
        "Kapoor", "Bhatia", "Agarwal", "Banerjee", "Chatterjee", "Mukherjee", "Das", "Bose",
        "Ghosh", "Sen", "Mishra", "Pandey", "Tiwari", "Srivastava", "Saxena", "Chauhan", "Yadav",
        "Naidu", "Krishnan", "Subramanian", "Venkatesan", "Raman", "Hegde", "Shetty", "Kamath",
        "Prasad", "Jain", "Bhatt", "Trivedi", "Dutta", "Sinha", "Khanna", "Arora", "Gill",
        "Sandhu", "Qureshi", "Ahmed",
    ),
    "JP": (
        "Sato", "Suzuki", "Takahashi", "Tanaka", "Watanabe", "Ito", "Yamamoto", "Nakamura",
        "Kobayashi", "Kato", "Yoshida", "Yamada", "Sasaki", "Yamaguchi", "Matsumoto", "Inoue",
        "Kimura", "Hayashi", "Shimizu", "Yamazaki", "Mori", "Abe", "Ikeda", "Hashimoto",
        "Yamashita", "Ishikawa", "Nakajima", "Maeda", "Fujita", "Ogawa", "Goto", "Okada",
        "Hasegawa", "Murakami", "Kondo", "Ishii", "Saito", "Sakamoto", "Endo", "Aoki", "Fujii",
        "Nishimura", "Fukuda", "Ota", "Miura", "Fujiwara", "Okamoto", "Matsuda", "Nakagawa",
        "Nakano", "Harada", "Ono", "Tamura", "Takeuchi",
    ),
    "CN": (
        "Wang", "Li", "Zhang", "Liu", "Chen", "Yang", "Huang", "Zhao", "Wu", "Zhou", "Xu", "Sun",
        "Ma", "Zhu", "Hu", "Guo", "He", "Lin", "Gao", "Luo", "Zheng", "Liang", "Xie", "Song",
        "Tang", "Han", "Feng", "Deng", "Cao", "Peng", "Zeng", "Xiao", "Tian", "Dong", "Pan",
        "Yuan", "Cai", "Jiang", "Yu", "Du", "Ye", "Cheng", "Wei", "Su", "Lu", "Ding", "Ren",
        "Shen", "Yao", "Jin", "Fan", "Fu", "Qian", "Dai", "Xia", "Zhong", "Wan",
    ),
    "BR": (
        "Silva", "Santos", "Oliveira", "Souza", "Rodrigues", "Ferreira", "Alves", "Pereira",
        "Lima", "Gomes", "Costa", "Ribeiro", "Martins", "Carvalho", "Almeida", "Lopes", "Soares",
        "Fernandes", "Vieira", "Barbosa", "Rocha", "Dias", "Nascimento", "Andrade", "Moreira",
        "Nunes", "Marques", "Machado", "Mendes", "Freitas", "Cardoso", "Ramos", "Gonçalves",
        "Santana", "Teixeira", "Araújo", "Pinto", "Correia", "Monteiro", "Moura", "Cavalcanti",
        "Batista", "Campos", "Castro", "Azevedo", "Barros", "Farias", "Reis", "Pires", "Fonseca",
        "Brandão", "Siqueira",
    ),
    "MX": (
        "Hernández", "García", "Martínez", "López", "González", "Pérez", "Rodríguez", "Sánchez",
        "Ramírez", "Cruz", "Flores", "Gómez", "Morales", "Vázquez", "Reyes", "Jiménez", "Torres",
        "Díaz", "Gutiérrez", "Ruiz", "Mendoza", "Aguilar", "Ortiz", "Moreno", "Castillo",
        "Romero", "Álvarez", "Méndez", "Chávez", "Rivera", "Juárez", "Ramos", "Domínguez",
        "Herrera", "Medina", "Castro", "Vargas", "Guzmán", "Velázquez", "Muñoz", "Rojas",
        "Contreras", "Salazar", "Luna", "Ortega", "Guerrero", "Estrada", "Cervantes", "Bautista",
        "Delgado", "Soto", "Navarro", "Escobar", "Cortés", "Ríos", "Zamora",
    ),
    "NG": (
        "Okafor", "Adeyemi", "Okonkwo", "Balogun", "Eze", "Ogunleye", "Nwosu", "Adebayo", "Obi",
        "Afolabi", "Chukwu", "Oyelaran", "Ibe", "Ogundipe", "Nwachukwu", "Bello", "Uzoma",
        "Akinola", "Okeke", "Adewale", "Onyeka", "Olawale", "Nwankwo", "Fashola", "Ezeh",
        "Oladipo", "Iwu", "Abiodun", "Okoro", "Babalola", "Anyanwu", "Ogbu", "Adeleke", "Onuoha",
        "Salami", "Ekwueme", "Olatunji", "Mbah", "Alabi", "Nnadi", "Ajayi", "Ugwu", "Oyewole",
        "Emenike", "Lawal", "Ibrahim", "Abdullahi", "Mohammed", "Danjuma", "Umar", "Okon",
        "Effiong",
    ),
    "KR": (
        "Kim", "Lee", "Park", "Choi", "Jung", "Kang", "Cho", "Yoon", "Jang", "Lim", "Han", "Oh",
        "Seo", "Shin", "Kwon", "Hwang", "Ahn", "Song", "Jeon", "Hong", "Yoo", "Ko", "Moon",
        "Yang", "Son", "Bae", "Baek", "Heo", "Nam", "Shim", "Noh", "Ha", "Kwak", "Sung", "Cha",
        "Joo", "Woo", "Koo", "Min", "Ryu", "Na", "Jin", "Ji", "Uhm", "Chae", "Won", "Chun",
        "Bang", "Gong", "Hyun", "Pyo", "Yeo", "Byun", "Ma",
    ),
}  # fmt: skip

DOUBLE_SURNAME_RATE: dict[str, float] = {"ES": 0.35, "MX": 0.3, "BR": 0.15}
"""Iberian and Latin American naming customs: many people carry two surnames."""

COUNTRY_LOCALES: dict[str, tuple[tuple[str, float], ...]] = {
    "US": (
        ("US", 0.50), ("MX", 0.10), ("IN", 0.10), ("CN", 0.08), ("KR", 0.05), ("NG", 0.05),
        ("UK", 0.03), ("DE", 0.02), ("FR", 0.02), ("ES", 0.02), ("JP", 0.02), ("BR", 0.01),
    ),
    "CA": (
        ("US", 0.52), ("FR", 0.12), ("UK", 0.10), ("IN", 0.10), ("CN", 0.08), ("KR", 0.04),
        ("NG", 0.04),
    ),
    "GB": (
        ("UK", 0.65), ("IN", 0.12), ("NG", 0.08), ("FR", 0.04), ("DE", 0.03), ("ES", 0.03),
        ("CN", 0.03), ("US", 0.02),
    ),
    "DE": (("DE", 0.82), ("FR", 0.03), ("ES", 0.03), ("UK", 0.03), ("IN", 0.04), ("CN", 0.03),
           ("US", 0.02)),
    "FR": (("FR", 0.82), ("ES", 0.04), ("DE", 0.03), ("NG", 0.03), ("UK", 0.03), ("IN", 0.03),
           ("CN", 0.02)),
    "ES": (("ES", 0.85), ("FR", 0.04), ("MX", 0.04), ("UK", 0.03), ("DE", 0.02), ("BR", 0.02)),
    "IN": (("IN", 0.97), ("UK", 0.02), ("US", 0.01)),
    "JP": (("JP", 0.94), ("US", 0.02), ("CN", 0.02), ("KR", 0.02)),
    "KR": (("KR", 0.95), ("US", 0.03), ("CN", 0.02)),
    "CN": (("CN", 0.97), ("US", 0.02), ("UK", 0.01)),
    "SG": (("CN", 0.55), ("IN", 0.20), ("UK", 0.10), ("US", 0.06), ("JP", 0.04), ("KR", 0.05)),
    "AU": (("UK", 0.55), ("US", 0.15), ("CN", 0.12), ("IN", 0.10), ("KR", 0.04), ("JP", 0.04)),
    "BR": (("BR", 0.92), ("ES", 0.03), ("JP", 0.03), ("US", 0.02)),
    "MX": (("MX", 0.90), ("ES", 0.05), ("US", 0.05)),
}  # fmt: skip

COUNTRY_REGION: dict[str, str] = {
    "US": "NA",
    "CA": "NA",
    "GB": "EU",
    "DE": "EU",
    "FR": "EU",
    "ES": "EU",
    "IN": "APAC",
    "JP": "APAC",
    "KR": "APAC",
    "CN": "APAC",
    "SG": "APAC",
    "AU": "APAC",
    "BR": "LATAM",
    "MX": "LATAM",
}
"""Coarse regions used by region-restricted applications (``Location.region``)."""

REGIONS: tuple[str, ...] = ("NA", "EU", "APAC", "LATAM")


def _weighted[T](rng: random.Random, options: tuple[tuple[T, float], ...]) -> T:
    values = [value for value, _ in options]
    weights = [weight for _, weight in options]
    return rng.choices(values, weights=weights, k=1)[0]


def locale_for_country(rng: random.Random, country: str) -> str:
    """A name locale plausible for someone working in ``country``."""
    return _weighted(rng, COUNTRY_LOCALES[country])


def make_name(rng: random.Random, locale: str) -> str:
    """A display name ("First Last") drawn from one locale group."""
    first = rng.choice(FIRST_NAMES[locale])
    last = rng.choice(LAST_NAMES[locale])
    if rng.random() < DOUBLE_SURNAME_RATE.get(locale, 0.0):
        second = rng.choice(LAST_NAMES[locale])
        if second != last:
            last = f"{last} {second}"
    return f"{first} {last}"


# --------------------------------------------------------------------------------------------
# Titles
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DepartmentTitles:
    """Canonical titles for one department, by level of the org chart."""

    head: str
    director: str
    manager: str
    ics: tuple[tuple[str, float], ...]
    """Individual-contributor titles with relative frequencies (seniority mix)."""
    intern: str
    vendor: str


DEPARTMENT_TITLES: dict[str, DepartmentTitles] = {
    "Executive": DepartmentTitles(
        head="Chief Executive Officer",
        director="Chief of Staff",
        manager="Executive Office Manager",
        ics=(
            ("Executive Assistant", 3.0),
            ("Senior Executive Assistant", 1.0),
            ("Executive Business Partner", 1.0),
            ("Strategy Associate", 1.0),
            ("Senior Strategy Associate", 0.5),
        ),
        intern="Strategy Intern",
        vendor="Executive Search Consultant",
    ),
    "Finance": DepartmentTitles(
        head="Chief Financial Officer",
        director="Director of Financial Planning and Analysis",
        manager="Finance Manager",
        ics=(
            ("Financial Analyst", 3.0),
            ("Senior Financial Analyst", 2.0),
            ("FP&A Analyst", 1.5),
            ("Treasury Analyst", 1.0),
            ("Senior Treasury Analyst", 0.5),
            ("Financial Analyst II", 1.0),
        ),
        intern="Finance Intern",
        vendor="Finance Consultant",
    ),
    "Accounting": DepartmentTitles(
        head="Corporate Controller",
        director="Director of Accounting",
        manager="Accounting Manager",
        ics=(
            ("Staff Accountant", 3.0),
            ("Senior Accountant", 2.0),
            ("General Ledger Accountant", 1.5),
            ("Accounts Payable Specialist", 1.5),
            ("Revenue Accountant", 1.0),
            ("Payroll Accountant", 0.8),
        ),
        intern="Accounting Intern",
        vendor="Accounting Consultant",
    ),
    "Procurement": DepartmentTitles(
        head="Head of Procurement",
        director="Director of Procurement",
        manager="Procurement Manager",
        ics=(
            ("Buyer", 3.0),
            ("Senior Buyer", 1.5),
            ("Procurement Specialist", 2.0),
            ("Category Analyst", 1.0),
            ("Senior Procurement Specialist", 0.8),
        ),
        intern="Procurement Intern",
        vendor="Sourcing Consultant",
    ),
    "Sales": DepartmentTitles(
        head="Vice President of Sales",
        director="Director of Sales",
        manager="Sales Manager",
        ics=(
            ("Account Executive", 4.0),
            ("Senior Account Executive", 2.0),
            ("Sales Development Representative", 3.0),
            ("Sales Engineer", 1.5),
            ("Sales Operations Analyst", 1.0),
            ("Senior Sales Engineer", 0.7),
        ),
        intern="Sales Intern",
        vendor="Channel Sales Consultant",
    ),
    "Marketing": DepartmentTitles(
        head="Vice President of Marketing",
        director="Director of Marketing",
        manager="Marketing Manager",
        ics=(
            ("Marketing Specialist", 3.0),
            ("Senior Marketing Specialist", 1.5),
            ("Product Marketing Manager", 1.5),
            ("Marketing Operations Analyst", 1.0),
            ("Content Strategist", 1.0),
            ("Graphic Designer", 1.0),
        ),
        intern="Marketing Intern",
        vendor="Agency Marketing Consultant",
    ),
    "Customer Support": DepartmentTitles(
        head="Vice President of Customer Support",
        director="Director of Customer Support",
        manager="Support Manager",
        ics=(
            ("Customer Support Representative", 4.0),
            ("Senior Customer Support Representative", 1.5),
            ("Technical Support Engineer", 2.0),
            ("Senior Technical Support Engineer", 1.0),
            ("Customer Success Manager", 1.5),
        ),
        intern="Customer Support Intern",
        vendor="Outsourced Support Agent",
    ),
    "Engineering": DepartmentTitles(
        head="Vice President of Engineering",
        director="Director of Engineering",
        manager="Engineering Manager",
        ics=(
            ("Software Engineer", 3.0),
            ("Software Engineer II", 3.0),
            ("Senior Software Engineer", 3.0),
            ("Staff Software Engineer", 1.0),
            ("Site Reliability Engineer", 1.5),
            ("Senior Site Reliability Engineer", 0.8),
            ("Quality Assurance Engineer", 1.0),
            ("DevOps Engineer", 1.0),
        ),
        intern="Software Engineering Intern",
        vendor="Contract Development Consultant",
    ),
    "Product": DepartmentTitles(
        head="Vice President of Product",
        director="Director of Product Management",
        manager="Group Product Manager",
        ics=(
            ("Product Manager", 3.0),
            ("Senior Product Manager", 2.0),
            ("Product Designer", 2.0),
            ("Senior Product Designer", 1.0),
            ("UX Researcher", 1.0),
            ("Technical Program Manager", 1.0),
        ),
        intern="Product Management Intern",
        vendor="UX Research Consultant",
    ),
    "Data": DepartmentTitles(
        head="Head of Data",
        director="Director of Data and Analytics",
        manager="Analytics Manager",
        ics=(
            ("Data Analyst", 3.0),
            ("Senior Data Analyst", 1.5),
            ("Data Engineer", 2.0),
            ("Senior Data Engineer", 1.5),
            ("Data Scientist", 1.5),
            ("Analytics Engineer", 1.0),
        ),
        intern="Data Science Intern",
        vendor="Data Engineering Consultant",
    ),
    "IT": DepartmentTitles(
        head="Chief Information Officer",
        director="Director of IT",
        manager="IT Manager",
        ics=(
            ("IT Support Specialist", 3.0),
            ("Senior IT Support Specialist", 1.5),
            ("Systems Administrator", 2.0),
            ("Senior Systems Administrator", 1.0),
            ("Network Engineer", 1.0),
            ("Identity and Access Management Engineer", 1.0),
            ("IT Asset Coordinator", 0.7),
        ),
        intern="IT Intern",
        vendor="Managed Services Technician",
    ),
    "Security": DepartmentTitles(
        head="Chief Information Security Officer",
        director="Director of Security Operations",
        manager="Security Manager",
        ics=(
            ("Security Analyst", 3.0),
            ("Senior Security Analyst", 1.5),
            ("Security Engineer", 2.0),
            ("Senior Security Engineer", 1.0),
            ("Governance, Risk and Compliance Analyst", 1.0),
        ),
        intern="Security Intern",
        vendor="Penetration Testing Consultant",
    ),
    "HR": DepartmentTitles(
        head="Chief People Officer",
        director="Director of People Operations",
        manager="HR Manager",
        ics=(
            ("HR Generalist", 2.0),
            ("Senior HR Generalist", 1.0),
            ("Recruiter", 2.0),
            ("Senior Recruiter", 1.0),
            ("HR Business Partner", 1.0),
            ("Payroll Specialist", 1.0),
            ("People Operations Coordinator", 1.0),
        ),
        intern="People Operations Intern",
        vendor="Recruiting Consultant",
    ),
    "Legal": DepartmentTitles(
        head="General Counsel",
        director="Associate General Counsel",
        manager="Legal Operations Manager",
        ics=(
            ("Corporate Counsel", 2.0),
            ("Senior Corporate Counsel", 1.0),
            ("Paralegal", 2.0),
            ("Contracts Manager", 1.5),
            ("Compliance Analyst", 1.5),
        ),
        intern="Legal Intern",
        vendor="Outside Counsel",
    ),
    "Operations": DepartmentTitles(
        head="Vice President of Operations",
        director="Director of Operations",
        manager="Operations Manager",
        ics=(
            ("Operations Analyst", 2.0),
            ("Senior Operations Analyst", 1.0),
            ("Logistics Coordinator", 3.0),
            ("Warehouse Associate", 3.0),
            ("Receiving Clerk", 1.5),
            ("Facilities Coordinator", 1.0),
        ),
        intern="Operations Intern",
        vendor="Third-Party Logistics Associate",
    ),
}

STARTUP_HEAD_PREFIX = "Head of"
"""Startups title department leads "Head of <Department>" instead of VP/C-level titles."""


def startup_head_title(department: str) -> str:
    """Canonical title of a department lead in a flat (startup) org."""
    if department == "Executive":
        return DEPARTMENT_TITLES["Executive"].head
    return f"{STARTUP_HEAD_PREFIX} {department}"


# Surface-form noise. Each entry maps a canonical phrase to its common directory spellings.
# Longer phrases come first so "Vice President" is handled before "President"-like fragments.
_ABBREVIATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Chief Executive Officer", ("CEO",)),
    ("Chief Financial Officer", ("CFO",)),
    ("Chief Information Security Officer", ("CISO",)),
    ("Chief Information Officer", ("CIO",)),
    ("Chief People Officer", ("CPO", "Chief People Off.")),
    ("Financial Planning and Analysis", ("FP&A", "FP and A")),
    ("Identity and Access Management", ("IAM", "Identity & Access Mgmt")),
    ("Governance, Risk and Compliance", ("GRC", "Governance, Risk & Compliance")),
    ("Quality Assurance", ("QA",)),
    ("Site Reliability Engineer", ("SRE",)),
    ("Sales Development Representative", ("SDR", "Sales Dev Rep")),
    ("Customer Support Representative", ("CSR", "Cust. Support Rep", "Support Rep")),
    ("Accounts Payable", ("AP", "A/P")),
    ("General Ledger", ("GL",)),
    ("Vice President of", ("VP", "VP,", "VP of", "V.P.")),
    ("Vice President", ("VP", "V.P.")),
    ("Human Resources", ("HR",)),
    ("Senior", ("Sr.", "Sr", "Snr", "SR.", "Snr.")),
    ("Manager", ("Mgr", "Mgr.", "Mngr")),
    ("Director", ("Dir.", "Dir")),
    ("Accountant", ("Acct", "Acct.", "Accts")),
    ("Engineering", ("Eng", "Engg.")),
    ("Engineer", ("Eng", "Engr", "Eng.")),
    ("Specialist", ("Spec.", "Spclst", "Spec")),
    ("Representative", ("Rep", "Rep.")),
    ("Administrator", ("Admin", "Admin.")),
    ("Associate", ("Assoc.", "Assoc")),
    ("Assistant", ("Asst.", "Asst")),
    ("Coordinator", ("Coord.", "Coord")),
    ("Operations", ("Ops", "Ops.")),
    ("Marketing", ("Mktg", "Mktg.")),
    ("Customer", ("Cust.", "Cust")),
    ("Technical", ("Tech", "Tech.")),
    ("Software", ("SW", "Sw.")),
    ("Executive", ("Exec", "Exec.")),
    ("Financial", ("Fin.", "Finance")),
    ("Management", ("Mgmt", "Mgmt.")),
    ("Information", ("Info",)),
    ("Development", ("Dev",)),
    ("Analytics", ("Analytics", "Anlytcs")),
    ("Analyst", ("Analyst", "Anlst")),
    ("Corporate", ("Corp.", "Corp")),
    ("Procurement", ("Procurement", "Proc.")),
)

_LEVEL_VARIANTS: dict[str, tuple[str, ...]] = {
    "II": ("II", "2", "ii", "Level 2"),
    "III": ("III", "3", "iii", "Level 3"),
}

_X_OF_Y = re.compile(r"^(?P<x>.+?) of (?P<y>.+)$")


def noisy_title(rng: random.Random, canonical: str) -> str:
    """A realistic surface variant of a canonical title ("Senior Accountant" -> "Sr. Acct").

    About a quarter of the time the canonical form is returned unchanged; otherwise
    abbreviations, word-order changes ("Director of Finance" -> "Finance Director"),
    "and"/"&" swaps, level-suffix variants and casing changes are applied at random.
    """
    if rng.random() < 0.25:
        return canonical
    title = canonical

    match = _X_OF_Y.match(title)
    if match and not title.startswith(("Vice President of", "Head of")) and rng.random() < 0.4:
        x, y = match.group("x"), match.group("y")
        title = rng.choice((f"{y} {x}", f"{x}, {y}", f"{x} - {y}"))

    for phrase, variants in _ABBREVIATIONS:
        if phrase in title and rng.random() < 0.55:
            title = re.sub(rf"\b{re.escape(phrase)}\b", rng.choice(variants), title, count=1)

    if rng.random() < 0.5:
        if " and " in title:
            title = title.replace(" and ", " & ")
        elif " & " in title:
            title = title.replace(" & ", " and ")

    words = title.split(" ")
    if words[-1] in _LEVEL_VARIANTS and rng.random() < 0.5:
        words[-1] = rng.choice(_LEVEL_VARIANTS[words[-1]])
        title = " ".join(words)

    roll = rng.random()
    if roll < 0.08:
        title = title.lower()
    elif roll < 0.11:
        title = title.upper()
    elif roll < 0.16:
        title = title.title()

    title = re.sub(r"\s+", " ", title).strip(" ,-")
    return title or canonical
