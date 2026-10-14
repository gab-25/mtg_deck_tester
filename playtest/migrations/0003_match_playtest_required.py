import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("playtest", "0002_forge_playtests"),
    ]

    operations = [
        migrations.AlterField(
            model_name="match",
            name="playtest",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="matches", to="playtest.playtest"
            ),
        ),
    ]
